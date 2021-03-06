"""
Copyright (c) 2018 Ewan Barr <ebarr@mpifr-bonn.mpg.de>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import logging
import coloredlogs
import json
import tornado
import signal
import time
import numpy as np
import ipaddress
import mosaic
from threading import Lock
from optparse import OptionParser
from tornado.gen import Return, coroutine
from katcp import Sensor, Message, AsyncDeviceServer, KATCPClientResource, AsyncReply
from katcp.kattypes import request, return_reply, Int, Str, Discrete, Float
from katportalclient import KATPortalClient
from katpoint import Antenna, Target
from mpikat.master_controller import MasterController
from mpikat.katportalclient_wrapper import KatportalClientWrapper
from mpikat.apsuse_worker_wrapper import ApsWorkerPool
from mpikat.utils import parse_csv_antennas, is_power_of_two, next_power_of_two
from mpikat.exceptions import ProductLookupError, ProductExistsError

# ?halt message means shutdown everything and power off all machines

log = logging.getLogger("mpikat.apsuse_master_controller")
lock = Lock()

class ApsMasterController(MasterController):
    """This is the main KATCP interface for the APSUSE
    pulsar searching system on MeerKAT. This controller only
    holds responsibility for capture of data from the CBF
    network and writing of that data to disk.

    This interface satisfies the following ICDs:
    CAM-APSUSE: <link>
    """
    VERSION_INFO = ("mpikat-aps-api", 0, 1)
    BUILD_INFO = ("mpikat-aps-implementation", 0, 1, "rc1")
    DEVICE_STATUSES = ["ok", "degraded", "fail"]
    def __init__(self, ip, port, dummy=False):
        """
        @brief       Construct new ApsMasterController instance

        @params  ip       The IP address on which the server should listen
        @params  port     The port that the server should bind to
        """
        super(ApsMasterController, self).__init__(ip, port, ApsWorkerPool())
        self._dummy = dummy
        if self._dummy:
            for ii in range(16):
                self._server_pool.add("127.0.0.1", 50000+ii)

    @request(Str(), Str(), Int(), Str(), Str())
    @return_reply()
    def request_configure(self, req, product_id, streams_json, proxy_name):
        """
        @brief      Configure APSUSE to receive and process data from a subarray

        @detail     REQUEST ?configure product_id antennas_csv n_channels streams_json proxy_name
                    Configure APSUSE for the particular data products

        @param      req               A katcp request object

        @param      product_id        This is a name for the data product, which is a useful tag to include
                                      in the data, but should not be analysed further. For example "array_1_bc856M4k".

        @param      streams_json      a JSON struct containing config keys and values describing the streams.

                                      For example:

                                      @code
                                         {'stream_type1': {
                                             'stream_name1': 'stream_address1',
                                             'stream_name2': 'stream_address2',
                                             ...},
                                             'stream_type2': {
                                             'stream_name1': 'stream_address1',
                                             'stream_name2': 'stream_address2',
                                             ...},
                                          ...}
                                      @endcode

                                      The steam type keys indicate the source of the data and the type, e.g. cam.http.
                                      stream_address will be a URI.  For SPEAD streams, the format will be spead://<ip>[+<count>]:<port>,
                                      representing SPEAD stream multicast groups. When a single logical stream requires too much bandwidth
                                      to accommodate as a single multicast group, the count parameter indicates the number of additional
                                      consecutively numbered multicast group ip addresses, and sharing the same UDP port number.
                                      stream_name is the name used to identify the stream in CAM.
                                      A Python example is shown below, for five streams:
                                      One CAM stream, with type cam.http.  The camdata stream provides the connection string for katportalclient
                                      (for the subarray that this APSUSE instance is being configured on).
                                      One F-engine stream, with type:  cbf.antenna_channelised_voltage.
                                      One X-engine stream, with type:  cbf.baseline_correlation_products.
                                      Two beam streams, with type: cbf.tied_array_channelised_voltage.  The stream names ending in x are
                                      horizontally polarised, and those ending in y are vertically polarised.

                                      @code
                                         pprint(streams_dict)
                                         {'cam.http':
                                             {'camdata':'http://10.8.67.235/api/client/1'},
                                          'cbf.antenna_channelised_voltage':
                                             {'i0.antenna-channelised-voltage':'spead://239.2.1.150+15:7148'},
                                          ...}
                                      @endcode

                                      If using katportalclient to get information from CAM, then reconnect and re-subscribe to all sensors
                                      of interest at this time.

        @param      proxy_name        The CAM name for the instance of the APSUSE data proxy that is being configured.
                                      For example, "APSUSE_3".  This can be used to query sensors on the correct proxy,
                                      in the event that there are multiple instances in the same subarray.

        @note       A configure call will result in the generation of a new subarray instance in APSUSE that will be added to the clients list.

        @return     katcp reply object [[[ !configure ok | (fail [error description]) ]]]
        """

        msg = ("Configuring new APSUSE product",
            "Product ID: {}".format(product_id),
            "Streams: {}".format(streams_json),
            "Proxy name: {}".format(proxy_name))
        log.info("\n".join(msg))
        # Test if product_id already exists
        if product_id in self._products:
            return ("fail", "APS already has a configured product with ID: {}".format(product_id))
        # Determine number of nodes required based on number of antennas in subarray
        # Note this is a poor way of handling this that may be updated later. In theory
        # there is a throughput measure as a function of bandwidth, polarisations and number
        # of antennas that allows one to determine the number of nodes to run. Currently we
        # just assume one antennas worth of data per NIC on our servers, so two antennas per
        # node.
        streams = json.loads(streams_json)
        try:
            streams['cam.http']['camdata']
        except KeyError as error:
            return ("fail", "JSON streams object does not contain required key: {}".format(str(error)))

        # TODO: change this request to @async_reply and make the whole thing a coroutine
        @coroutine
        def configure():
            kpc = self._katportal_wrapper_type(streams['cam.http']['camdata'])
            # Get all antenna observer strings
            log.debug("Fetching FBFUSE and subarray configuration information")
            raise NotImplementedw
            self._products[product_id] = product
            self._update_products_sensor()
            req.reply("ok",)
            log.debug("Configured APSUSE instance with ID: {}".format(product_id))
        self.ioloop.add_callback(configure)
        raise AsyncReply

    @request(Str())
    @return_reply()
    def request_deconfigure(self, req, product_id):
        """
        @brief      Deconfigure the APSUSE instance.

        @note       Deconfigure the APSUSE instance. If APSUSE uses katportalclient to get information
                    from CAM, then it should disconnect at this time.

        @param      req               A katcp request object

        @param      product_id        This is a name for the data product, used to track which subarray is being deconfigured.
                                      For example "array_1_bc856M4k".

        @return     katcp reply object [[[ !deconfigure ok | (fail [error description]) ]]]
        """
        log.info("Deconfiguring APSUSE instace with ID '{}'".format(product_id))
        # Test if product exists
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        try:
            product.deconfigure()
        except Exception as error:
            return ("fail", str(error))
        del self._products[product_id]
        self._update_products_sensor()
        return ("ok",)


    @request(Str(), Str())
    @return_reply()
    @coroutine
    def request_target_start(self, req, product_id, target):
        """
        @brief      Notify APSUSE that a new target is being observed

        @param      product_id      This is a name for the data product, used to track which subarray is being deconfigured.
                                    For example "array_1_bc856M4k".

        @param      target          A KATPOINT target string

        @return     katcp reply object [[[ !target-start ok | (fail [error description]) ]]]
        """
        log.info("Received new target: {}".format(target))
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            raise Return(("fail", str(error)))
        try:
            target = Target(target)
        except Exception as error:
            raise Return(("fail", str(error)))
        yield product.target_start(target)
        raise Return(("ok",))


    # DELETE this

    @request(Str())
    @return_reply()
    @coroutine
    def request_target_stop(self, req, product_id):
        """
        @brief      Notify APSUSE that the telescope has stopped observing a target

        @param      product_id      This is a name for the data product, used to track which subarray is being deconfigured.
                                    For example "array_1_bc856M4k".

        @return     katcp reply object [[[ !target-start ok | (fail [error description]) ]]]
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            raise Return(("fail", str(error)))
        yield product.target_stop()
        raise Return(("ok",))


    @request(Str())
    @return_reply()
    def request_capture_init(self, req, product_id):
        """NOOP"""
        return ("ok",)

    @request(Str())
    @return_reply()
    def request_capture_done(self, req, product_id):
        """NOOP"""
        return ("ok",)

    @request(Str())
    @return_reply()
    def request_capture_start(self, req, product_id):
        """
        @brief      Request that APSUSE start beams streaming

        @detail     Upon this call the provided coherent and incoherent beam configurations will be evaluated
                    to determine if they are physical and can be met with the existing hardware. If the configurations
                    are acceptable then servers allocated to this instance will be triggered to begin production of beams.

        @param      req               A katcp request object

        @param      product_id        This is a name for the data product, used to track which subarray is being deconfigured.
                                      For example "array_1_bc856M4k".

        @return     katcp reply object [[[ !start-beams ok | (fail [error description]) ]]]
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        @coroutine
        def start():
            try:
                product.capture_start()
            except Exception as error:
                req.reply("fail", str(error))
            else:
                req.reply("ok",)
        self.ioloop.add_callback(start)
        raise AsyncReply

    @request(Str(), Str())
    @return_reply()
    def request_provision_beams(self, req, product_id, sb_id):
        """
        @brief      Request that APSUSE asynchronously prepare to start beams streaming

        @detail     Upon this call the provided coherent and incoherent beam configurations will be evaluated
                    to determine if they are physical and can be met with the existing hardware. If the configurations
                    are acceptable then servers allocated to this instance will be triggered to prepare for the production of beams.
                    Unlike a call to ?capture-start, ?provision-beams will not trigger a connection to multicast groups and will not
                    wait for completion before returning, instead it will start the process of beamformer resource alloction and compilation.
                    To determine when the process is complete, the user must wait on the value of the product "state" sensor becoming "ready",
                    e.g.

                    @code
                        client.sensor['{}-state'.format(proxy_name)].wait(
                            lambda reading: reading.value == 'ready')
                    @endcode

        @param      req               A katcp request object

        @param      product_id        This is a name for the data product, used to track which subarray is being deconfigured.
                                      For example "array_1_bc856M4k".

        @param      sb_id             Schedule block ID for the commencing schedule block

        @return     katcp reply object [[[ !start-beams ok | (fail [error description]) ]]]
        """
        # Note: the state of the product won't be updated until the start call hits the top of the
        # event loop. It may be preferable to keep a self.starting_future object and yield on it
        # in capture-start if it exists. The current implementation may or may not be a bug...
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        # This check needs to happen here as this call
        # should return immediately
        if not product.idle:
            return ("fail", "Can only provision beams on an idle APS product")
        self.ioloop.add_callback(lambda : product.prepare(sb_id))
        return ("ok",)

    @request(Str())
    @return_reply()
    def request_capture_stop(self, req, product_id):
        """
        @brief      Stop APSUSE streaming

        @param      product_id      This is a name for the data product, used to track which subarray is being deconfigured.
                                    For example "array_1_bc856M4k".
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        @coroutine
        def stop():
            product.capture_stop()
            req.reply("ok",)
        self.ioloop.add_callback(stop)
        raise AsyncReply

    @request(Str(), Str(), Int())
    @return_reply()
    def request_set_configuration_authority(self, req, product_id, hostname, port):
        """
        @brief     Set the configuration authority for an APS product

        @detail    The parameters passed here specify the address of a server that
                   can be triggered to provide APSUSE with configuration information
                   at schedule block and target boundaries. The configuration authority
                   must be a valid KATCP server.
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        product.set_configuration_authority(hostname, port)
        return ("ok",)

    @request(Str())
    @return_reply()
    def request_reset_beams(self, req, product_id):
        """
        @brief      Reset the positions of all allocated beams

        @note       This call may only be made AFTER a successful call to start-beams. Before this point no beams are
                    allocated to the instance. If all beams are currently allocated an exception will be raised.

        @param      req             A katcp request object

        @param      product_id      This is a name for the data product, used to track which subarray is being deconfigured.
                                    For example "array_1_bc856M4k".

        @return     katcp reply object [[[ !reset-beams m ok | (fail [error description]) ]]]
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        else:
            beam = product.reset_beams()
            return ("ok", )

    @request(Str(), Str())
    @return_reply(Str())
    def request_add_beam(self, req, product_id, target):
        """
        @brief      Configure the parameters of one beam

        @note       This call may only be made AFTER a successful call to start-beams. Before this point no beams are
                    allocated to the instance. If all beams are currently allocated an exception will be raised.

        @param      req             A katcp request object

        @param      product_id      This is a name for the data product, used to track which subarray is being deconfigured.
                                    For example "array_1_bc856M4k".

        @param      target          A KATPOINT target string

        @return     katcp reply object [[[ !add-beam ok | (fail [error description]) ]]]
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        try:
            target = Target(target)
        except Exception as error:
            return ("fail", str(error))
        beam = product.add_beam(target)
        return ("ok", beam.idx)

    @request(Str(), Str(), Int(), Float(), Float(), Float())
    @return_reply(Str())
    def request_add_tiling(self, req, product_id, target, nbeams, reference_frequency, overlap, epoch):
        """
        @brief      Configure the parameters of a static beam tiling

        @note       This call may only be made AFTER a successful call to start-beams. Before this point no beams are
                    allocated to the instance. If there are not enough free beams to satisfy the request an
                    exception will be raised.

        @note       Beam shapes calculated for tiling are always assumed to be 2D elliptical Gaussians.

        @param      req             A katcp request object

        @param      product_id      This is a name for the data product, used to track which subarray is being deconfigured.
                                    For example "array_1_bc856M4k".

        @param      target          A KATPOINT target string

        @param      nbeams          The number of beams in this tiling pattern.

        @param      reference_frequency     The reference frequency at which to calculate the synthesised beam shape,
                                            and thus the tiling pattern. Typically this would be chosen to be the
                                            centre frequency of the current observation.

        @param      overlap         The desired overlap point between beams in the pattern. The overlap defines
                                    at what power point neighbouring beams in the tiling pattern will meet. For
                                    example an overlap point of 0.1 corresponds to beams overlapping only at their
                                    10%-power points. Similarly a overlap of 0.5 corresponds to beams overlapping
                                    at their half-power points. [Note: This is currently a tricky parameter to use
                                    when values are close to zero. In future this may be define in sigma units or
                                    in multiples of the FWHM of the beam.]

        @param      epoch           The desired epoch for the tiling pattern as a unix time. A typical usage would
                                    be to set the epoch to half way into the coming observation in order to minimise
                                    the effect of parallactic angle and array projection changes altering the shape
                                    and position of the beams and thus changing the efficiency of the tiling pattern.


        @return     katcp reply object [[[ !add-tiling ok | (fail [error description]) ]]]
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        try:
            target = Target(target)
        except Exception as error:
            return ("fail", str(error))
        tiling = product.add_tiling(target, nbeams, reference_frequency, overlap, epoch)
        return ("ok", tiling.idxs())

    @request()
    @return_reply(Int())
    def request_product_list(self, req):
        """
        @brief      List all currently registered products and their states

        @param      req               A katcp request object

        @note       The details of each product are provided via an #inform
                    as a JSON string containing information on the product state.

        @return     katcp reply object [[[ !product-list ok | (fail [error description]) <number of configured products> ]]],
        """
        for product_id,product in self._products.items():
            info = {}
            info[product_id] = product.info()
            as_json = json.dumps(info)
            req.inform(as_json)
        return ("ok",len(self._products))

    @request(Str(), Str())
    @return_reply()
    def request_set_default_target_configuration(self, req, product_id, target):
        """
        @brief      Set the configuration of APSUSE from the APSUSE configuration server

        @param      product_id      This is a name for the data product, used to track which subarray is being deconfigured.
                                    For example "array_1_bc856M4k".

        @param      target          A KATPOINT target string
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        try:
            target = Target(target)
        except Exception as error:
            return ("fail", str(error))
        if not product.capturing:
            return ("fail","Product must be capturing before a target confiugration can be set.")
        product.reset_beams()
        # TBD: Here we connect to some database and request the default configurations
        # For example this may return secondary target in the FoV
        #
        # As a default the current system will put one beam directly on target and
        # the rest of the beams in a static tiling pattern around this target
        now = time.time()
        nbeams = product._beam_manager.nbeams
        product.add_tiling(target, nbeams-1, 1.4e9, 0.5, now)
        product.add_beam(target)
        return ("ok",)

    @request(Str(), Str())
    @return_reply()
    def request_set_default_sb_configuration(self, req, product_id, sb_id):
        """
        @brief      Set the configuration of APSUSE from the APSUSE configuration server

        @param      product_id      This is a name for the data product, used to track which subarray is being deconfigured.
                                    For example "array_1_bc856M4k".

        @param      sb_id           The schedule block ID. Decisions of the configuarion of APSUSE will be made dependent on
                                    the configuration of the current subarray, the primary and secondary science projects
                                    active and the targets expected to be visted during the execution of the schedule block.
        """
        try:
            product = self._get_product(product_id)
        except ProductLookupError as error:
            return ("fail", str(error))
        if product.capturing:
            return ("fail", "Cannot reconfigure a currently capturing instance.")
        product.configure_coherent_beams(400, product._katpoint_antennas, 1, 16)
        product.configure_incoherent_beam(product._katpoint_antennas, 1, 16)
        now = time.time()
        nbeams = product._beam_manager.nbeams
        product.add_tiling(target, nbeams-1, 1.4e9, 0.5, now)
        product.add_beam(target)
        return ("ok",)

@coroutine
def on_shutdown(ioloop, server):
    log.info("Shutting down server")
    yield server.stop()
    ioloop.stop()

def main():
    usage = "usage: %prog [options]"
    parser = OptionParser(usage=usage)
    parser.add_option('-H', '--host', dest='host', type=str,
        help='Host interface to bind to')
    parser.add_option('-p', '--port', dest='port', type=long,
        help='Port number to bind to')
    parser.add_option('', '--log_level',dest='log_level',type=str,
        help='Port number of status server instance',default="INFO")
    parser.add_option('', '--dummy',action="store_true", dest='dummy',
        help='Set status server to dummy')
    (opts, args) = parser.parse_args()
    logger = logging.getLogger('mpikat')
    coloredlogs.install(
        fmt="[ %(levelname)s - %(asctime)s - %(name)s - %(filename)s:%(lineno)s] %(message)s",
        level=opts.log_level.upper(),
        logger=logger)
    logging.getLogger('katcp').setLevel('INFO')
    ioloop = tornado.ioloop.IOLoop.current()
    log.info("Starting ApsMasterController instance")
    server = ApsMasterController(opts.host, opts.port, dummy=opts.dummy)
    signal.signal(signal.SIGINT, lambda sig, frame: ioloop.add_callback_from_signal(
        on_shutdown, ioloop, server))
    def start_and_display():
        server.start()
        log.info("Listening at {0}, Ctrl-C to terminate server".format(server.bind_address))

    ioloop.add_callback(start_and_display)
    ioloop.start()

if __name__ == "__main__":
    main()

