import numpy as np
from mpi4py import MPI
import sys,petsc4py
petsc4py.init(sys.argv)
from petsc4py import PETSc
comm = MPI.COMM_WORLD

# from dmplex_grad   import DMPlexGrad

class SurfMesh(object):

    def __init__(self):
        pass

    def update_surface_processes(self, rainfall_pattern, sediment_distribution):
        rainfall_pattern = np.array(rainfall_pattern)
        sediment_distribution = np.array(sediment_distribution)
        if rainfall_pattern.size != self.npoints or sediment_distribution.size != self.npoints:
            raise IndexError("Incompatible array size, should be {}".format(self.npoints))

        from time import clock
        self.rainfall_pattern = rainfall_pattern.copy()
        self.sediment_distribution = sediment_distribution.copy()

        # cumulative flow
        t = clock()
        self.upstream_area = self.cumulative_flow(self.area) # err - this is number of triangles
        self.timings['cumulative flow'] = [clock()-t, self.log.getCPUTime(), self.log.getFlops()]
        if self.verbose:
            print(" - Cumulative flow {}s".format(clock()-t))

        # Find low points
        self.low_points = self.identify_low_points()

        # Find high points
        self.outflow_points = self.identify_outflow_points()


    def handle_low_points(self, base, its):
        self.low_points = self.identify_low_points()

        if len(self.low_points) == 0:
            return self.height

        delta_height = self.lvec.duplicate()

        fixed = 0
        rejected = 0

        for point in self.low_points:
            if self.height[point] - base < 0.005*(self.height.max() - base):
                rejected += 1
                continue

            # find the mean height in the neighbourhood and fill up everything nearby
            fixed += 1
            delta_height[point] = self.height[self.neighbour_array_lo_hi[point]].mean()
            if self.verbose:
                print "{} Old height {} -> {}".format(point, self.height[point], delta_height[point])


        # Now march the new height to all the uphill nodes of these nodes
        height = np.maximum(self.height, delta_height.array)

        self.dm.localToGlobal(delta_height, self.gvec)
        global_dH = self.gvec.copy()

        for p in range(0, its):
            self.downhillMat.multTranspose(global_dH, self.gvec)
            global_dH.setArray(self.gvec)
            global_dH.scale(1.001)

        self.dm.globalToLocal(global_dH, delta_height)


        height = np.maximum(self.height, delta_height.array)

        if self.verbose:
            print "Updated {} points".format(fixed)
            print "Rejected {} points".format(rejected)

        return height



    def identify_low_points(self):
        """
        Identify if the mesh has (internal) local minima and return an array of node indices
        """

        nodes = np.arange(0, self.npoints, dtype=np.int)
        low_nodes = self.neighbour_array_2_low[:,0]
        mask = np.logical_and(nodes == low_nodes, self.bmask == True)

        return low_nodes[mask]

    def identify_high_points(self):
        """
        Identify if the mesh has (internal) local minima and return an array of node indices
        """

        high_point_list = []
        for node in xrange(0,self.npoints):
            if self.neighbour_array_lo_hi[node][-1] == node and self.bmask[node] == True:
                high_point_list.append(node)

        return np.array(high_point_list)

    
    def identify_outflow_points(self):
        """
        Identify the (boundary) outflow points and return an array of node indices
        """

        nodes = np.arange(0, self.npoints, dtype=np.int)
        low_nodes = self.neighbour_array_2_low[:,0]
        mask = np.logical_and(nodes == low_nodes, self.bmask == False)

        return low_nodes[mask]


    def identify_flat_spots(self):

        smooth_grad1 = self.local_area_smoothing(self.slope, its=1, centre_weight=0.5)
        flat_spot_field = np.where(smooth_grad1 < smooth_grad1.max()/10, 0.0, 1.0)
        flat_spots = np.where(smooth_grad1 < smooth_grad1.max()/10, True, False)

        return flat_spots


    def stream_power_erosion_deposition_rate(self, efficiency=0.01, smooth_power=3, \
                                             smooth_low_points=2, smooth_erosion_rate=2, \
                                             smooth_deposition_rate=2, smooth_operator=None,
                                             centre_weight_u=0.5, centre_weight=0.5):

        """
        Function of the SurfaceProcessMesh which computes stream-power erosion and deposition rates
        from a given rainfall pattern (self.rainfall_pattern).

        In this model we assume a the carrying capacity of the stream is related to the stream power and so is the
        erosion rate. The two are related to one another in this particular case by a single contant (everywhere on the mesh)
        This does not allow for spatially variable erodability and it does not allow for differences in the dependence
        of erosion / deposition on the stream power.

        Deposition occurs such that the upstream-integrated eroded sediment does not exceed the carrying capacity at a given
        point. To conserve mass, we have to treat internal drainage points carefully and, optionally, smooth the deposition
        upstream of the low point. We also have to be careful when stream-power and carrying capacity increase going downstream.
        This produces a negative deposition rate when the flow is at capacity. We suppress this behaviour and balance mass across
        all other deposition sites but this does mean the capacity is not perfectly satisfied everywhere.

        parameters:
         efficiency=0.01          : erosion rate for a given stream power compared to carrying capacity
         smooth_power=3           : upstream / downstream smoothing of the stream power (number of cycles of smoothing)
         smooth_low_points=3      : upstream smoothing of the deposition at low points (number of cycles of smoothing)
         smooth_erosion_rate=0    : upstream / downstream smoothing of the computed erosion rate (number of cycles of smoothing)
         smooth_deposition_rate=0 : upstream / downstream smoothing of the computed erosion rate (number of cycles of smoothing)

        """


        if smooth_operator == None:
            smooth_operator = self.streamwise_smoothing

        # Calculate stream power

        rainflux = self.rainfall_pattern
        rainfall = self.area * rainflux
        cumulative_rain = self.cumulative_flow(rainfall)
        cumulative_flow_rate = cumulative_rain / self.area
        stream_power = self.uphill_smoothing(cumulative_flow_rate * self.slope, smooth_power, centre_weight=centre_weight_u)

        #  predicted erosion rate from stream power * efficiency
        #  maximum sediment that can be transported is limited by the local carrying capacity (assume also prop to stream power)
        #  whatever cannot be passed on has to be deposited

        erosion_rate = self.streamwise_smoothing(efficiency * stream_power, smooth_erosion_rate, centre_weight=centre_weight)
        full_capacity_sediment_flux = stream_power
        full_capacity_sediment_load = stream_power * self.area
        cumulative_eroded_material = self.cumulative_flow(self.area * erosion_rate)

        # But this can exceed the carrying capacity

        transport_limited_eroded_material = np.minimum(cumulative_eroded_material, full_capacity_sediment_load)
        transport_limited_erosion_rate = transport_limited_eroded_material / self.area

        # And this therefore implies a deposition rate which reduces the total sediment in the system to capacity
        # Calculate this by substracting the deposited amounts from the excess integrated flow. We could then iterate
        # to compute the new erosion rates etc, but here we just spread the sediments around to places where
        # the deposition is positive

        excess = self.gvec.duplicate()
        deposition = self.lvec.duplicate()
        self.lvec.setArray(cumulative_eroded_material - transport_limited_eroded_material)
        self.dm.localToGlobal(self.lvec, excess)
        self.downhillMat.mult(excess, self.gvec)
        self.dm.globalToLocal(excess - self.gvec, deposition)
        depo_sum = deposition.sum()


        # Now rebalance the fact that we have clipped off the negative deposition which will need
        # to be clawed back downstream (ideally, but for now we can just make a global correction)

        deposition = np.clip(deposition.array, 0.0, 1.0e99)
        deposition *= depo_sum / (depo_sum + 1e-12)


        # The (interior) low points are a bit of a problem - we stomped on the stream power there
        # but this produces a very lumpy deposition at the low point itself and this could (does)
        # make the numerical representation pretty unstable. Instead what we can do is to take that
        # deposition at the low points let it spill into the local area


        ## These will instead be handled by a specific routine "handle_low_points" which is
        ## done once the height has been updated

        if len(self.low_points):
            deposition[self.low_points] = 0.0

        # The flat regions in the domain are also problematic since the deposition there is

        flat_spots = self.identify_flat_spots()

        if len(flat_spots):
            smoothed_deposition = deposition.copy()
            smoothed_deposition[np.invert(flat_spots)] = 0.0
            smoothed_deposition = self.local_area_smoothing(smoothed_deposition, its=2, centre_weight=0.5)
            deposition[flat_spots] = smoothed_deposition[flat_spots]

        deposition_rate = smooth_operator(deposition, smooth_deposition_rate, centre_weight=centre_weight) / self.area

        return erosion_rate, deposition_rate, stream_power



    def landscape_diffusion_critical_slope(self, kappa, critical_slope, fluxBC):
        '''
        Non-linear diffusion to keep slopes at a critical value. Assumes a background
        diffusion rate (can be a vector of length mesh.tri.npoints) and a critical slope value.

        This term is suitable for the sloughing of sediment from hillslopes.

        To Do: The critical slope should be a function of the material (sediment, basement etc)
        but currently it is not.

        To Do: The fluxBC flag is global ... it should apply to the outward normal
        at selected nodes but currently it is set to kill both fluxes at all boundary nodes.
        '''

        inverse_bmask = np.invert(self.bmask)

        kappa_eff = kappa / (1.01 - (np.clip(self.slope,0.0,critical_slope) / critical_slope)**2)
        diff_timestep   =  self.area.min() / kappa_eff.max()


        gradZx, gradZy = self.derivative_grad(self.height)
        gradZx = self._local_global_local(gradZx)
        gradZy = self._local_global_local(gradZy)
        flux_x = kappa_eff * gradZx
        flux_y = kappa_eff * gradZy
        if fluxBC:
            flux_x[inverse_bmask] = 0.0
            flux_y[inverse_bmask] = 0.0  # outward normal flux, actually

        diffDz = self.derivative_div(flux_x, flux_y)
        diffDz = self._local_global_local(diffDz)

        if not fluxBC:
            diffDz[inverse_bmask] = 0.0

        return diffDz, diff_timestep