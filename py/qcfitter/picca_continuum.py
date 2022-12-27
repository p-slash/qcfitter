import numpy as np
import fitsio
from scipy.optimize import minimize, curve_fit
from scipy.interpolate import UnivariateSpline

from mpi4py import MPI

from qcfitter.mpi_utils import logging_mpi
from qcfitter.mathtools import Fast1DInterpolator

class PiccaContinuumFitter(object):
    """ Picca continuum fitter. Fits spectra without coadding.
    Pipeline ivar is smoothed before using in weights.
    Mean continuum is smoothed using inverse weights and cubic spline.
    """
    def _set_fiducials(self, fiducial_fits):
        if self.mpi_rank == 0:
            with fitsio.FITS(fiducial_fits) as fts:
                data = fts['STATS'].read()
            waves = data['LAMBDA']
            waves_0 = waves[0]
            dwave = waves[1]-waves[0]
            nsize = waves.size
            if not np.allclose(np.diff(waves), dwave):
                raise Exception(
                    "Failed to construct fiducial mean flux and varlss from "
                    f"{fiducial_fits}::LAMBDA is not equally spaced."
                )

            meanflux = np.array(data['MEANFLUX'], dtype='d')
            varlss   = np.array(data['VAR'], dtype='d')
        else:
            waves_0 = 0.
            dwave   = 0.
            nsize   = 0

        nsize, waves_0, dwave = self.comm.bcast([nsize, waves_0, dwave])

        if self.mpi_rank != 0:
            meanflux = np.empty(nsize, dtype='d')
            varlss   = np.empty(nsize, dtype='d')

        self.comm.Bcast([meanflux, MPI.DOUBLE])
        self.comm.Bcast([varlss, MPI.DOUBLE])

        self.meanflux_interp = Fast1DInterpolator(waves_0, dwave,
            meanflux)
        self.varlss_interp = Fast1DInterpolator(waves_0, dwave,
            varlss, np.zeros(nsize))

    def __init__(self, w1rf, w2rf, dwrf, w1obs, w2obs, nwbins=20, fiducial_fits=None):
        self.nbins = int((w2rf-w1rf)/dwrf)+1
        self.rfwave, self.dwrf = np.linspace(w1rf, w2rf, self.nbins, retstep=True)
        self._denom = np.log(self.rfwave[-1]/self.rfwave[0])

        self.meancont_interp = Fast1DInterpolator(w1rf, self.dwrf,
            np.ones(self.nbins), np.zeros(self.nbins))

        self.comm = MPI.COMM_WORLD
        self.mpi_rank = self.comm.Get_rank()

        if fiducial_fits:
            self._set_fiducials(fiducial_fits)
            self.varlss_fitter = None
        else:
            self.meanflux_interp = Fast1DInterpolator(0., 1., np.ones(3))
            self.varlss_fitter = VarLSSFitter(w1obs, w2obs, nwbins)
            self.varlss_interp = Fast1DInterpolator(w1obs, self.varlss_fitter.dwobs,
                0.1*np.ones(nwbins), np.zeros(nwbins))

    def _continuum_costfn(self, x, wave, flux, ivar_sm, z_qso):
        cost = 0

        for arm, wave_arm in wave.items():
            cont_est  = self.get_continuum_model(x, wave_arm/(1+z_qso))
            # no_neg = np.sum(cont_est<0)
            # penalty = wave_arm.size * no_neg**2

            cont_est *= self.meanflux_interp(wave_arm)

            var_lss = self.varlss_interp(wave_arm)*cont_est**2
            weight  = ivar_sm[arm] / (1+ivar_sm[arm]*var_lss)
            w = weight > 0

            cost += np.dot(weight, (flux[arm] - cont_est)**2) - np.log(weight[w]).sum()# + penalty

        return cost

    def get_continuum_model(self, x, wave_rf_arm):
        Slope = np.log(wave_rf_arm/self.rfwave[0])/self._denom

        cont = self.meancont_interp(wave_rf_arm) * (x[0] + x[1]*Slope)
        # Multiply with resolution
        # Edges are difficult though

        return cont

    def fit_continuum(self, spec):
        # constr = ({'type': 'eq',
        #            'fun': lambda x: self.get_continuum_model(x, wave_arm/(1+spec.z_qso))
        #           } for wave_arm in spec.forestwave.values() if wave_arm.size>0
        # )

        result = minimize(
            self._continuum_costfn,
            spec.cont_params['x'],
            args=(spec.forestwave,
                  spec.forestflux,
                  spec.forestivar_sm,
                  spec.z_qso),
            # constraints=constr,
            # method='COBYLA',
            method='L-BFGS-B',
            bounds=[(0, None), (None, None)],
            jac=None
        )

        spec.cont_params['valid'] = result.success

        if result.success:
            for wave_arm in spec.forestwave.values():
                _cont = self.get_continuum_model(result.x, wave_arm/(1+spec.z_qso))

                if any(_cont<0):
                    spec.cont_params['valid'] = False
                    break

        if spec.cont_params['valid']:
            spec.cont_params['x']    = result.x
            spec.cont_params['xcov'] = result.hess_inv.todense()
            # spec.cont_params['chi2'] = result.fun
            spec.cont_params['cont'] = {}
            chi2 = 0
            for arm, wave_arm in spec.forestwave.items():
                cont_est  = self.get_continuum_model(result.x, wave_arm/(1+spec.z_qso))
                cont_est *= self.meanflux_interp(wave_arm)
                spec.cont_params['cont'][arm] = cont_est

                var_lss = self.varlss_interp(wave_arm)*cont_est**2
                weight  = spec.forestivar_sm[arm] / (1+spec.forestivar_sm[arm]*var_lss)

                chi2 += np.dot(weight, (spec.forestflux[arm] - cont_est)**2)
            spec.cont_params['chi2'] = chi2
        else:
            spec.cont_params['cont'] = None

    def fit_continua(self, spectra_list):
        no_valid_fits=0
        no_invalid_fits=0

        # For each forest fit continuum
        for spec in spectra_list:
            self.fit_continuum(spec)

            if not spec.cont_params['valid']:
                no_invalid_fits+=1
                # logging_mpi(f"mpi:{mpi_rank}:Invalid continuum TARGETID: {spec.targetid}.", 0)
            else:
                no_valid_fits += 1

        no_valid_fits = self.comm.reduce(no_valid_fits, root=0)
        no_invalid_fits = self.comm.reduce(no_invalid_fits, root=0)
        logging_mpi(f"Number of valid fits: {no_valid_fits}", self.mpi_rank)
        logging_mpi(f"Number of invalid fits: {no_invalid_fits}", self.mpi_rank)

    def update(self, spectra_list):
        norm_flux = np.zeros(self.nbins)
        counts = np.zeros(self.nbins)
        if self.varlss_fitter is not None:
            self.varlss_fitter.reset()

        for spec in spectra_list:
            if not spec.cont_params['valid']:
                continue 

            for arm, wave_arm in spec.forestwave.items():
                wave_rf_arm = wave_arm/(1+spec.z_qso)
                bin_idx = ((wave_rf_arm - self.rfwave[0])/self.dwrf + 0.5).astype(int)

                cont  = spec.cont_params['cont'][arm]
                flux_ = spec.forestflux[arm]/cont
                # Deconvolve resolution matrix ?

                var_lss = self.varlss_interp(wave_arm)
                weight  = spec.forestivar_sm[arm]*cont**2
                weight  = weight / (1+weight*var_lss)

                norm_flux += np.bincount(bin_idx, weights=flux_*weight, minlength=self.nbins)
                counts    += np.bincount(bin_idx, weights=weight, minlength=self.nbins)

                if self.varlss_fitter is not None:
                    self.varlss_fitter.add(wave_arm, flux_-1, spec.forestivar[arm]*cont**2)

        self.comm.Allreduce(MPI.IN_PLACE, norm_flux)
        self.comm.Allreduce(MPI.IN_PLACE, counts)
        norm_flux /= counts
        std_flux = 1/np.sqrt(counts)

        # Smooth new estimates
        spl = UnivariateSpline(self.rfwave, norm_flux, w=1/std_flux)
        norm_flux = spl(self.rfwave)
        oldcont = self.meancont_interp.fp.copy()
        self.meancont_interp.fp *= norm_flux

        # remove tilt
        x = np.log(self.rfwave/self.rfwave[0])/self._denom
        P1 = 2*x-1
        B = 3*np.trapz(self.meancont_interp.fp*P1, x=x)
        self.meancont_interp.fp -= B * P1

        # normalize
        mean_ = np.trapz(self.meancont_interp.fp, x=x)
        self.meancont_interp.fp /= mean_
        norm_flux = self.meancont_interp.fp/oldcont - 1
        std_flux /= mean_
        self.meancont_interp.ep = std_flux

        logging_mpi("Continuum updates", self.mpi_rank)
        sl = np.s_[::max(1, int(self.nbins/10))]
        logging_mpi("wave_rf \t| update \t| error", self.mpi_rank)
        for w, n, e in zip(self.rfwave[sl], norm_flux[sl], std_flux[sl]):
            logging_mpi(f"{w:7.2f}\t| {n:7.2e}\t| pm {e:7.2e}", self.mpi_rank)

        has_converged = np.all(np.abs(norm_flux) < 0.33*std_flux)
        # np.allclose(np.abs(norm_flux), std_flux, rtol=0.5)

        if self.varlss_fitter is not None:
            logging_mpi("Fitting var_lss", self.mpi_rank)
            y, ep = self.varlss_fitter.fit(self.varlss_interp.fp, self.comm, self.mpi_rank)
            self.varlss_interp.fp = y
            self.varlss_interp.ep = ep
            sl = np.s_[::max(1, int(y.size/10))]
            logging_mpi("wave_obs \t| var_lss \t| error", self.mpi_rank)
            for w, v, e in zip(self.varlss_fitter.waveobs[sl], y[sl], ep[sl]):
                logging_mpi(f"{w:7.2f}\t| {v:7.2e} \t| {e:7.2e}", self.mpi_rank)

        return has_converged

    def iterate(self, spectra_list, niterations, outdir=None):
        has_converged = False

        for spec in spectra_list:
            spec.cont_params['method'] = 'picca'
            spec.cont_params['dof']    = spec.get_real_size()

        if self.mpi_rank == 0 and outdir:
            fattr = fitsio.FITS(f"{outdir}/attributes.fits", 'rw', clobber=True)

        for it in range(niterations):
            logging_mpi(f"Fitting iteration {it+1}/{niterations}", self.mpi_rank)

            if self.mpi_rank == 0 and outdir:
                self.save(fattr, it+1)

            # Fit all continua one by one
            self.fit_continua(spectra_list)
            # Stack all spectra in each process
            # Broadcast and recalculate global functions
            has_converged = self.update(spectra_list)

            if has_converged:
                logging_mpi("Iteration has converged.", self.mpi_rank)
                break

        if not has_converged:
            logging_mpi("Iteration has NOT converged.", self.mpi_rank, "warning")

        if self.mpi_rank == 0 and outdir:
            fattr.close()

    def save(self, fattr, it):
        fattr.write([self.rfwave, self.meancont_interp.fp, self.meancont_interp.ep],
            names=['lambda_rf', 'mean_cont', 'e_mean_cont'], extname=f'CONT-{it}')
        fattr.write([self.varlss_fitter.waveobs, self.varlss_interp.fp, self.varlss_interp.ep],
            names=['lambda', 'var_lss', 'e_var_lss'], extname=f'VAR_FUNC-{it}')

class VarLSSFitter(object):
    min_no_pix = 500
    min_no_qso = 50

    @staticmethod
    def variance_function(var_pipe, var_lss, eta=1):
        return eta*var_pipe + var_lss

    def __init__(self, w1obs, w2obs, nwbins, var1=1e-5, var2=2., nvarbins=100):
        self.nwbins = nwbins
        self.nvarbins = nvarbins
        self.waveobs, self.dwobs = np.linspace(w1obs, w2obs, nwbins, retstep=True)
        self.ivar_edges   = np.logspace(-np.log10(var2), -np.log10(var1), nvarbins+1)
        self.ivar_centers = (self.ivar_edges[1:]+self.ivar_edges[:-1])/2

        self.minlength = (self.nvarbins+2) * (self.nwbins+2)
        self.var_delta = np.zeros(self.minlength)
        self.mean_delta = np.zeros_like(self.var_delta)
        self.var2_delta = np.zeros_like(self.var_delta)
        self.num_pixels = np.zeros_like(self.var_delta, dtype=int)
        self.num_qso = np.zeros_like(self.var_delta, dtype=int)

    def reset(self):
        self.var_delta = 0.
        self.mean_delta = 0.
        self.var2_delta = 0.
        self.num_pixels = 0
        self.num_qso = 0

    def add(self, wave, delta, ivar):
        # add 1 to match searchsorted/bincount output/input
        wave_indx = ((wave - self.waveobs[0])/self.dwobs + 0.5).astype(int) + 1
        ivar_indx = np.searchsorted(self.ivar_edges, ivar)
        all_indx = ivar_indx + wave_indx * (self.nvarbins+2)

        self.mean_delta += np.bincount(all_indx, weights=delta, minlength=self.minlength)
        self.var_delta  += np.bincount(all_indx, weights=delta**2, minlength=self.minlength)
        self.var2_delta += np.bincount(all_indx, weights=delta**4, minlength=self.minlength)
        rebin = np.bincount(all_indx, minlength=self.minlength)
        self.num_pixels += rebin
        rebin[rebin>0] = 1
        self.num_qso += rebin

    def _allreduce(self, comm):
        comm.Allreduce(MPI.IN_PLACE, self.mean_delta)
        comm.Allreduce(MPI.IN_PLACE, self.var_delta)
        comm.Allreduce(MPI.IN_PLACE, self.var2_delta)
        comm.Allreduce(MPI.IN_PLACE, self.num_pixels)
        comm.Allreduce(MPI.IN_PLACE, self.num_qso)

        w = self.num_pixels > 0
        self.var_delta[w] /= self.num_pixels[w]
        self.mean_delta[w] /= self.num_pixels[w]
        self.var_delta -= self.mean_delta**2
        self.var2_delta[w] /= self.num_pixels[w]
        self.var2_delta -= self.var_delta**2
        self.var2_delta[w] /= self.num_pixels[w]

    def fit(self, current_varlss, comm, mpi_rank):
        self._allreduce(comm)
        var_lss = np.zeros(self.nwbins)
        std_var_lss = np.zeros(self.nwbins)

        for iwave in range(self.nwbins):
            i1 = (iwave+1)*(self.nvarbins+2)
            i2 = i1 + self.nvarbins
            wbinslice = np.s_[i1:i2]

            w  = self.num_pixels[wbinslice] > VarLSSFitter.min_no_pix
            w &= self.num_qso[wbinslice] > VarLSSFitter.min_no_qso

            if w.sum() == 0:
                err_msg = ( "Not enough statistics for VarLSSFitter at"
                           f" wave_obs: {self.waveobs[iwave]:.2f}.")
                logging_mpi(err_msg, mpi_rank, "warning")
                continue

            try:
                pfit, pcov = curve_fit(
                    VarLSSFitter.variance_function,
                    1/self.ivar_centers[w],
                    self.var_delta[wbinslice][w],
                    p0=current_varlss[iwave],
                    sigma=np.sqrt(self.var2_delta[wbinslice][w]),
                    absolute_sigma=True,
                    check_finite=True,
                    bounds=(0, 2)
                )
            except Exception as e:
                err_msg = (f"VarLSSFitter failed at wave_obs: {self.waveobs[iwave]:.2f}. "
                           f"Reason: {e}. Extrapolating.")
                logging_mpi(err_msg, mpi_rank, "warning")
            else:
                var_lss[iwave] = pfit[0]
                std_var_lss[iwave] = np.sqrt(pcov[0, 0])

        # Smooth new estimates
        w = var_lss > 0
        spl = UnivariateSpline(self.waveobs[w], var_lss[w], w=1/std_var_lss[w])

        nfails = np.sum(var_lss == 0)
        if nfails > 0:
            logging_mpi(f"VarLSSFitter failed and extrapolated at {nfails} points.",
                    mpi_rank, "warning")

        return spl(self.waveobs), std_var_lss

