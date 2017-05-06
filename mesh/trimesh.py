import numpy as np
from mpi4py import MPI
import sys,petsc4py
petsc4py.init(sys.argv)
from petsc4py import PETSc
comm = MPI.COMM_WORLD
from time import clock
import stripy

try: range = xrange
except: pass


class TriMesh(object):
    """
    Creating a global vector from a distributed DM removes duplicate entries (shadow zones)
    We recommend having 1) triangle or 2) scipy installed for Delaunay triangulations.
    """
    def __init__(self, dm, verbose=True):
        from scipy.spatial import Delaunay
        self.timings = dict() # store times

        self.log = PETSc.Log()
        self.log.begin()

        self.verbose = verbose

        self.dm = dm
        self.gvec = dm.createGlobalVector()
        self.lvec = dm.createLocalVector()
        self.sect = dm.getDefaultSection()
        self.sizes = self.gvec.getSizes(), self.gvec.getSizes()
        
        lgmap_r = dm.getLGMap()
        l2g = lgmap_r.indices.copy()
        offproc = l2g < 0

        l2g[offproc] = -(l2g[offproc] + 1)
        lgmap_c = PETSc.LGMap().create(l2g, comm=comm)

        self.lgmap_row = lgmap_r
        self.lgmap_col = lgmap_c
        

        # Delaunay triangulation
        t = clock()
        coords = dm.getCoordinatesLocal().array.reshape(-1,2)
        self.tri = stripy.Triangulation(coords[:,0], coords[:,1])
        self.npoints = self.tri.npoints
        self.timings['triangulation'] = [clock()-t, self.log.getCPUTime(), self.log.getFlops()]
        if self.verbose:
            print(" - Delaunay triangulation {}s".format(clock()-t))

        
        # Calculate weigths and pointwise area
        t = clock()
        self.calculate_area_weights()
        self.timings['area weights'] = [clock()-t, self.log.getCPUTime(), self.log.getFlops()]
        if self.verbose:
            print(" - Calculate node weights and area {}s".format(clock()-t))


        # Calculate edge lengths
        t = clock()
        self.get_edge_lengths()
        self.timings['edge lengths'] = [clock()-t, self.log.getCPUTime(), self.log.getFlops()]
        if self.verbose:
            print(" - Compute edge lengths {}s".format(clock()-t))


        # Find neighbours
        t = clock()
        self.construct_neighbours()
        self.timings['construct neighbours'] = [clock()-t, self.log.getCPUTime(), self.log.getFlops()]
        if self.verbose:
            print(" - Construct neighbour array {}s".format(clock()-t))


        # Find boundary points
        t = clock()
        self.bmask = self.get_boundary()
        self.timings['find boundaries'] = [clock()-t, self.log.getCPUTime(), self.log.getFlops()]
        if self.verbose:
            print(" - Find boundaries {}s".format(clock()-t))


        # Build smoothing operator
        t = clock()
        self._build_smoothing_matrix()
        self.timings['smoothing matrix'] = [clock()-t, self.log.getCPUTime(), self.log.getFlops()]
        if self.verbose:
            print(" - Build smoothing matrix {}s".format(clock()-t))

        self.root = False


    def calculate_area_weights(self):
        """
        Calculate weigths and pointwise area
        """

        # Encircling vectors
        v1 = self.tri.points[self.tri.simplices[:,1]] - self.tri.points[self.tri.simplices[:,0]]
        v2 = self.tri.points[self.tri.simplices[:,2]] - self.tri.points[self.tri.simplices[:,1]]
        v3 = self.tri.points[self.tri.simplices[:,0]] - self.tri.points[self.tri.simplices[:,2]]

        self.triangle_area = 0.5*(v1[:,0]*v2[:,1] - v1[:,1]*v2[:,0])

        ntriw  = np.zeros(self.npoints)
        weight = np.zeros(self.npoints, dtype=PETSc.IntType)

        for t, triangle in enumerate(self.tri.simplices):
            weight[triangle] += 1
            ntriw[triangle] += abs(v1[t][0]*v3[t][1] - v1[t][1]*v3[t][0])

        self.weight = weight
        self.area = ntriw/6.0


    def node_neighbours(self, point):
        """
        Returns a list of neighbour nodes for a given point in the delaunay triangulation
        """

        return self.vertex_neighbour_vertices[1][self.vertex_neighbour_vertices[0][point]:self.vertex_neighbour_vertices[0][point+1]]


    def derivative_grad(self, PHI, nit=10, tol=1e-8):
        """
        Compute derivatives of PHI in the x, y directions.
        This routine uses SRFPACK to compute derivatives on a C-1 bivariate function.

        Arguments
        ---------
         PHI : ndarray of floats, shape (n,)
            compute the derivative of this array
         nit : int optional (default: 10)
            number of iterations to reach convergence
         tol : float optional (default: 1e-8)
            convergence is reached when this tolerance is met

        Returns
        -------
         PHIx : ndarray of floats, shape(n,)
            first partial derivative of PHI in x direction
         PHIy : ndarray of floats, shape(n,)
            first partial derivative of PHI in y direction
        """
        return self.tri.gradient(PHI, nit=10, tol=1e-8)


    def derivative_div(self, PHIx, PHIy, **kwargs):
        """
        Compute second order derivative from flux fields PHIx, PHIy
        We evaluate the gradient on these fields using the derivative-grad method.

        Arguments
        ---------
         PHIx : ndarray of floats, shape (n,)
            array of first partial derivatives in x direction
         PHIy : ndarray of floats, shape (n,)
            array of first partial derivatives in y direction
         kwargs : optional keyword-argument specifiers
            keyword arguments to be passed onto derivative_grad
            e.g. nit=5, tol=1e-3

        Returns
        -------
         del2PHI : ndarray of floats, shape (n,)
            second derivative of PHI
        """
        u_xx, u_xy = self.derivative_grad(PHIx, **kwargs)
        u_yx, u_yy = self.derivative_grad(PHIy, **kwargs)

        return u_xx + u_yy


    def get_edge_lengths(self):
        """
        Find all edges in a triangluation and their lengths
        """
        points = self.tri.points

        i1 = np.sort([self.tri.simplices[:,0], self.tri.simplices[:,1]], axis=0)
        i2 = np.sort([self.tri.simplices[:,0], self.tri.simplices[:,2]], axis=0)
        i3 = np.sort([self.tri.simplices[:,1], self.tri.simplices[:,2]], axis=0)

        a = np.hstack([i1, i2, i3]).T

        # find unique rows in numpy array 
        # <http://stackoverflow.com/questions/16970982/find-unique-rows-in-numpy-array>
        b = np.ascontiguousarray(a).view(np.dtype((np.void, a.dtype.itemsize * a.shape[1])))
        edges = np.unique(b).view(a.dtype).reshape(-1, a.shape[1])

        edge_lengths = np.linalg.norm(points[edges[:,0]] - points[edges[:,1]], axis=1)

        self.edges = edges
        self.edge_lengths = edge_lengths


    def construct_neighbours(self):
        """
        Find neighbours from edges and store as CSR coordinates.

        This allows you to directly ask the neighbours for a given node a la Qhull,
        or efficiently construct a sparse matrix (PETSc/SciPy)
        """

        row = np.hstack([self.edges[:,0], self.edges[:,1]])
        col = np.hstack([self.edges[:,1], self.edges[:,0]])
        val = np.hstack([self.edge_lengths, self.edge_lengths])

        # sort by row
        sort = row.argsort()
        row = row[sort].astype(PETSc.IntType)
        col = col[sort].astype(PETSc.IntType)
        val = val[sort]

        nnz = np.bincount(row) # number of nonzeros
        indptr = np.insert(np.cumsum(nnz),0,0)

        self.vertex_neighbours = nnz.astype(PETSc.IntType)
        self.vertex_neighbour_vertices = indptr, col
        self.vertex_neighbour_distance = val

        # We may not need this, but constuct anyway for now!
        neighbours = [[]]*self.npoints
        closed_neighbours = [[]]*self.npoints

        for i in range(indptr.size-1):
            start, end = indptr[i], indptr[i+1]
            neighbours[i] = np.array(col[start:end])
            closed_neighbours[i] = np.hstack([i, neighbours[i]])

        self.neighbour_list = np.array(neighbours)
        self.neighbour_array = np.array(closed_neighbours)


    def _build_smoothing_matrix(self):

        indptr, indices = self.vertex_neighbour_vertices
        weight  = 1.0/self.weight
        nweight = weight[indices]

        lgmask = self.lgmap_row.indices >= 0


        nnz = self.vertex_neighbours[lgmask] + 1

        # smoothMat = self.dm.createMatrix()
        # smoothMat.setOption(smoothMat.Option.NEW_NONZERO_LOCATIONS, False)
        smoothMat = PETSc.Mat().create(comm=comm)
        smoothMat.setType('aij')
        smoothMat.setSizes(self.sizes)
        smoothMat.setLGMap(self.lgmap_row, self.lgmap_col)
        smoothMat.setFromOptions()
        smoothMat.setPreallocationNNZ(nnz)

        # read in data 
        smoothMat.setValuesLocalCSR(indptr.astype(PETSc.IntType), indices.astype(PETSc.IntType), nweight)
        self.lvec.setArray(weight)
        self.dm.localToGlobal(self.lvec, self.gvec)
        smoothMat.setDiagonal(self.gvec)

        smoothMat.assemblyBegin()
        smoothMat.assemblyEnd()

        self.localSmoothMat = smoothMat


    def local_area_smoothing(self, data, its=1, centre_weight=0.75):

        self.lvec.setArray(data)
        self.dm.localToGlobal(self.lvec, self.gvec)
        smooth_data = self.gvec.copy()

        for i in range(0, its):
            self.localSmoothMat.mult(smooth_data, self.gvec)
            smooth_data = centre_weight*smooth_data + (1.0 - centre_weight)*self.gvec

        self.dm.globalToLocal(smooth_data, self.lvec)

        return self.lvec.array


    def get_boundary(self, marker="boundary"):
        """
        Find the nodes on the boundary from the DM
        If marker does not exist then the convex hull is used.
        """
        bmask = np.ones(self.npoints, dtype=bool)

        pStart, pEnd = self.dm.getDepthStratum(0)
        
        labels = []
        for i in range(self.dm.getNumLabels()):
            labels.append(self.dm.getLabelName(i))

        if marker not in labels:
            print("Warning! No boundary information in DMPlex.\nContinuing with convex hull.")
            self.dm.markBoundaryFaces(marker) # marks line segments
            boundary_points = self.tri.convex_hull() + pStart
            for pt in boundary_points:
                self.dm.setLabelValue(marker, pt, 1)

        boundaryIS = self.dm.getStratumIS(marker, 1)
        pt_range = np.logical_and(boundaryIS.indices >= pStart, boundaryIS.indices < pEnd)
        boundary_points = boundaryIS.indices[pt_range] - pStart

        bmask[boundary_points] = False
        return bmask


    def save_mesh_to_file(self, file):
        """
        Save mesh points to a HDF5 file.
        Requires h5py and a HDF5 built with at least the following options:
            ./configure --enable-parallel --enable-shared
        """
        import h5py

        if isinstance(file, basestring):
            if not file.endswith('.h5'):
                file = file + '.h5'

        on_proc = self.lgmap_row.indices >= 0
        indices = self.lgmap_row.indices[on_proc]

        gsize = self.gvec.getSizes()[1]

        with h5py.File(str(file), 'w', driver='mpio', comm=comm) as f:
            grp = f.create_group('dmplex')
            dset = grp.create_dataset('points', (gsize,2), dtype='f')
            dset[list(indices)] = self.tri.points[on_proc]


    def save_field_to_file(self, file, *args, **kwargs):
        """
        Save field to an HDF5 file.
        Maps the field from a local vector to global.
        """
        import h5py

        if isinstance(file, basestring):
            if not file.endswith('.h5'):
                file = file + '.h5'

        kwdict = kwargs
        for i, arg in enumerate(args):
            key = 'arr_%d' % i
            if key in kwdict.keys():
                raise ValueError("Cannot use un-named variables and keyword %s" % key)
            kwdict[key] = arg

        indices = self.lgmap_row.indices[self.lgmap_row.indices >= 0]
        gsize = self.gvec.getSizes()[1]
        lsize = self.lvec.getSizes()[1]

        with h5py.File(str(file), 'w', driver='mpio', comm=comm) as f:
            for key in kwdict:
                val = kwdict[key]
                if val.size == lsize:
                    self.lvec.setArray(val)
                    self.dm.localToGlobal(self.lvec, self.gvec)
                else:
                    self.gvec.setArray(val)
                dset = f.create_dataset(key, (gsize,), dtype='f')
                dset[list(indices)] = self.gvec.array


    def _gather_root(self):
        """
        MPI gather operation to root processor
        """
        self.tozero, self.zvec = PETSc.Scatter.toZero(self.gvec)


        # Gather x,y points
        pts = self.tri.points
        self.lvec.setArray(pts[:,0])
        self.dm.localToGlobal(self.lvec, self.gvec)
        self.tozero.scatter(self.gvec, self.zvec)

        self.root_x = self.zvec.array.copy()
        
        self.lvec.setArray(pts[:,1])
        self.dm.localToGlobal(self.lvec, self.gvec)
        self.tozero.scatter(self.gvec, self.zvec)

        self.root_y = self.zvec.array.copy()

        self.root = True # yes we have gathered everything


    def gather_data(self, data):
        """
        Gather data on root processor 
        """

        # check if we already gathered pts on root
        if not self.root:
            self._gather_root()

        self.lvec.setArray(data)
        self.dm.localToGlobal(self.lvec, self.gvec)
        self.tozero.scatter(self.gvec, self.zvec)

        return self.zvec.array.copy()


    def _local_global_local(self, vector):
        """ Communicate to global then back again """
        self.lvec.setArray(vector)
        self.dm.localToGlobal(self.lvec, self.gvec)
        self.dm.globalToLocal(self.gvec, self.lvec)
        return self.lvec.array.copy()
