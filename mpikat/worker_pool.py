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
from threading import Lock
from katcp import KATCPClientResource

log = logging.getLogger('mpikat.worker_pool')
lock = Lock()

class WorkerAllocationError(Exception):
    pass

class WorkerDeallocationError(Exception):
    pass

class WorkerPool(object):
    """Wrapper class for managing server
    allocation and deallocation to subarray/products
    """
    def __init__(self):
        """
        @brief   Construct a new instance
        """
        self._servers = set()
        self._allocated = set()

    def make_wrapper(self, hostname, port):
        raise NotImplemented

    def add(self, hostname, port):
        """
        @brief  Add a new FbfWorkerServer to the server pool

        @params hostname The hostname for the worker server
        @params port     The port number that the worker server serves on
        """
        log.debug("Adding {}:{} to worker pool".format(hostname, port))
        wrapper = self.make_wrapper(hostname,port)
        if not wrapper in self._servers:
            wrapper.start()
            log.debug("Adding {} to server set".format(wrapper))
            self._servers.add(wrapper)
        else:
            log.debug("Worker instance {} already exists".format(wrapper))
        log.debug("Added {}:{} to worker pool".format(hostname, port))

    def remove(self, hostname, port):
        """
        @brief  Add a new FbfWorkerServer to the server pool

        @params hostname The hostname for the worker server
        @params port     The port number that the worker server serves on
        """
        log.debug("Removing {}:{} from worker pool".format(hostname, port))
        wrapper = self.make_wrapper(hostname,port)
        if wrapper in self._allocated:
            raise WorkerDeallocationError("Cannot remove allocated server from pool")
        try:
            self._servers.remove(wrapper)
        except KeyError:
            log.warning("Could not find {}:{} in server pool".format(hostname, port))
        else:
            log.debug("Removed {}:{} from worker pool".format(hostname, port))

    def allocate(self, count):
        """
        @brief    Allocate a number of servers from the pool.

        @note     Free servers will be allocated by priority order
                  with 0 being highest priority

        @return   A list of FbfWorkerWrapper objects
        """
        with lock:
            log.debug("Request to allocate {} servers".format(count))
            available_servers = list(self._servers.difference(self._allocated))
            log.debug("{} servers available".format(len(available_servers)))
            available_servers.sort(key=lambda server: server.priority, reverse=True)
            if len(available_servers) < count:
                raise WorkerAllocationError("Cannot allocate {0} servers, only {1} available".format(
                    count, len(available_servers)))
            allocated_servers = []
            for _ in range(count):
                server = available_servers.pop()
                log.debug("Allocating server: {}".format(server))
                allocated_servers.append(server)
                self._allocated.add(server)
            return allocated_servers

    def deallocate(self, servers):
        """
        @brief    Deallocate servers and return the to the pool.

        @param    A list of Node objects
        """
        for server in servers:
            log.debug("Deallocating server: {}".format(server))
            self._allocated.remove(server)

    def reset(self):
        """
        @brief   Deallocate all servers
        """
        log.debug("Reseting server pool allocations")
        self._allocated = set()

    def available(self):
        """
        @brief   Return list of available servers
        """
        return list(self._servers.difference(self._allocated))

    def navailable(self):
        return len(self.available())

    def used(self):
        """
        @brief   Return list of allocated servers
        """
        return list(self._allocated)

    def nused(self):
        return len(self.used())


class WorkerWrapper(object):
    """Wrapper around a client to an FbfWorkerServer
    instance.
    """
    def __init__(self, hostname, port):
        """
        @brief  Create a new wrapper around a client to a worker server

        @params hostname The hostname for the worker server
        @params port     The port number that the worker server serves on
        """
        log.debug("Creating worker client to worker at {}:{}".format(hostname, port))
        self._client = KATCPClientResource(dict(
            name="worker-server-client",
            address=(hostname, port),
            controlled=True))
        self.hostname = hostname
        self.port = port
        self.priority = 0 # Currently no priority mechanism is implemented
        self._started = False

    def start(self):
        """
        @brief  Start the client to the worker server
        """
        log.debug("Starting client to worker at {}:{}".format(self.hostname, self.port))
        self._client.start()
        self._started = True

    def __repr__(self):
        return "<{} @ {}:{}>".format(self.__class__.__name__, self.hostname, self.port)

    def __hash__(self):
        # This has override is required to allow these wrappers
        # to be used with set() objects. The implication is that
        # the combination of hostname and port is unique for a
        # worker server
        return hash((self.hostname, self.port))

    def __eq__(self, other):
        # Also implemented to help with hashing
        # for sets
        return self.__hash__() == hash(other)

    def __del__(self):
        if self._started:
            try:
                self._client.stop()
            except Exception as error:
                log.exception(str(error))

