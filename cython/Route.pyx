from libcpp.memory cimport shared_ptr
from Route cimport Route

cdef class PyRoute:
    @staticmethod
    cdef PyRoute create(shared_ptr[Route] c_route):
        if not c_route:
            return None
        cdef PyRoute py_rt = PyRoute.__new__(PyRoute)
        py_rt._ptr = c_route
        return py_rt

    @property
    def prefix(self):
        return self._ptr.get().prefix.decode('utf-8')

    @property
    def ASPath(self):
        return self._ptr.get().ASPath.decode('utf-8')

    @property
    def origin(self):
        return self._ptr.get().origin.decode('utf-8')

    @property
    def community(self):
        return self._ptr.get().community.decode('utf-8')

    @property
    def localPref(self):
        return self._ptr.get().localPref

    @property
    def pathLength(self):
        return self._ptr.get().pathLength

    @property
    def originPriority(self):
        return self._ptr.get().originPriority
