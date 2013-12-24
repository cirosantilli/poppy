#!/usr/bin/env python

# package doc string now in __init__.py in this directory!


from __future__ import division
import multiprocessing
import copy
import numpy as np
import matplotlib.pyplot as plt
import scipy.special
import scipy.ndimage.interpolation
import matplotlib
import time
from matplotlib.colors import LogNorm  # for log scaling of images, with automatic colorbar support

import astropy.io.fits as fits



#from .utils import imshow_with_mouseover, estimate_optimal_nprocesses, fftw_load_wisdom, fftw_save_
from .matrixDFT import MatrixFourierTransform
from . import utils
from . import settings


import logging
_log = logging.getLogger('poppy')

try:
    from IPython.core.debugger import Tracer; stop = Tracer()
except:
    pass

# Setup infrastructure for FFTW
_FFTW_INIT = {}  # dict of array sizes for which we have already performed the required FFTW planning step
_FFTW_FLAGS = ['measure']
if settings.use_fftw():
    try:
        # try to import FFTW and use it
        import pyfftw
    except:
        # we tried but failed to import it. 
        settings.use_fftw.set(False)

# internal constants for types of plane
_PUPIL = 1
_IMAGE = 2
_DETECTOR = 3 # specialized type of image plane.
_ROTATION = 4 # not a real optic, just a coordinate transform
_typestrs = ['', 'Pupil plane', 'Image plane', 'Detector', 'Rotation']


#conversions
_RADIANStoARCSEC = 180.*60*60 / np.pi


#------ Utility functions for array padding ------

def padToOversample(array, oversample):
    """ Add zeros around the edge of an array, for a given desired FFT integer oversampling ratio

    Parameters
    ----------
    array :  ndarray
        A 2D array representing some image
    oversample : int
        Padding factor for expanding the array

    Returns
    -------
    padded_array : ndarray
        A larger array containing mostly zeros but with the input array in the center.

    See Also
    ---------
    padToSize
    """
    npix = array.shape[0]
    padded = np.zeros(shape=(npix*oversample, npix*oversample), dtype=array.dtype)
    n0 = float(npix)*(oversample - 1)/2
    n1 = n0+npix
    padded[n0:n1, n0:n1] = array
    return padded

def padToSize(array, padded_shape):
    """ Add zeros around the edge of an array, to reach a specific defined size and shape.
    This is similar to padToOversample but is more flexible.

    Parameters
    ----------
    array :  ndarray
        A 2D array representing some image
    padded_shape :  tuple of 2 elements
        Desired size for the padded array.

    Returns
    -------
    padded_array : ndarray
        A larger array containing mostly zeros but with the input array in the center.


    See Also
    ---------
    padToOversample 
    """
    #npix = array.shape[0]
    padded = np.zeros(shape=padded_shape, dtype=array.dtype)
    n0 = (padded_shape[0] - array.shape[0])/2  # pixel offset for the inner array
    m0 = (padded_shape[1] - array.shape[1])/2  # pixel offset in second dimension
    n1 = n0+array.shape[0]
    m1 = m0+array.shape[1]
    padded[n0:n1, m0:m1] = array
    return padded

def removePadding(array,oversample):
    " Remove zeros around the edge of an array, assuming some integer oversampling padding factor "
    npix = array.shape[0] / oversample
    n0 = float(npix)*(oversample - 1)/2
    n1 = n0+npix
    return array[n0:n1,n0:n1].copy()


#------ Utility functions for parallelization ------

def _wrap_propagate_for_multiprocessing(args):
    """ This is an internal helper routine for parallelizing computations across multiple processors.

    Python's multiprocessing module allows easy execution of tasks acrossg
    many CPUs or even distinct machines. It relies on Python's pickle mechanism to
    serialize and pass objects between processes. One side effect of this is
    that object instance methods cannot be pickled on their own, and thus cannot be easily
    invoked in other processes.

    Here, we work around that by pickling the entire object and argument list, packed
    as a tuple, transmitting that to the new process, and then unpickling that,
    unpacking the results, and *then* at last making our instance method call.
    """
    self, wavelength, weight, kwargs, usefftw3flag = args
    settings.use_fftw.set(usefftw3flag)  #passed in from parent process

    if settings.use_fftw(): # we're in a different Python interprepter process so we need to
                            # load the wisdom here too
        utils.fftw_load_wisdom()
    
    # not implemented yet? 


    return args[0].propagate_mono(wavelength, poly_weight=weight, save_intermediates=False, **kwargs)


#------ Wavefront class -----

class Wavefront(object):
    """ A class representing a monochromatic wavefront that can be transformed between
    pupil and image planes (but not to intermediate planes, yet).

    In a pupil plane, a wavefront object `wf` has

        * `wf.diam`,         a diameter in meters
        * `wf.pixelscale`,   a scale in meters/pixel

    In an image plane, it has

        * `wf.fov`,          a field of view in arcseconds
        * `wf.pixelscale`,   a  scale in arcsec/pixel


    Use the `wf.propagateTo()` method to transform a wavefront between conjugate planes. This will update those properties as appropriate.

    By default, `Wavefronts` are created in a pupil plane. Set `pixelscale=#` to make an image plane instead.

    Parameters
    ----------
    wavelength : float
        Wavelength of light in meters
    npix : int
        Size parameter for wavefront array to create, per side.
    diam : float, optional
        For _PUPIL wavefronts, sets physical size corresponding to npix. Units are meters.
        At most one of diam or pixelscale should be set when creating a wavefront.
    pixelscale : float, optional
        For _IMAGE PLANE wavefronts, use this pixel scale.
    oversample : int, optional
        how much to oversample by in FFTs. Default is 2.
        Note that final propagations to Detectors use a different algorithmg
        and, optionally, a separate oversampling factor.
    dtype : numpy.dtype, optional
        default is double complex.

    """

    def __init__(self,wavelength=2e-6, npix=1024, dtype=np.complex128, diam=8.0, oversample=2, pixelscale=None):

        if wavelength > 1e-4:
            raise ValueError("The specified wavelength %f is implausibly large. Remember to specify the desired wavelength in *meters*." % wavelength)

        self._last_transform_type=None # later used to track MFT vs FFT pixel coord centering in coordinates()
        self.oversample = oversample

        self.wavelength = float(wavelength)                 # wavelen in meters, obviously
        """Wavelength in meters """
        self.diam= float(diam)                              # pupil plane size in meters
        """Diameter in meters. Applies to a pupil plane only."""
        self.fov = None                                     # image plane size in arcsec
        """Field of view in arcsec. Applies to an image plane only."""
        self.pixelscale = None
        "Pixel scale, in arcsec/pixel or meters/pixel depending on plane type"

        if pixelscale is None:
            self.pixelscale = self.diam / npix                  # scale in meters/pix or arcsec/pix, as appropriate
            self.planetype = _PUPIL                              # are we at image or pupil?
        else:
            self.pixelscale = pixelscale
            self.planetype = _IMAGE
        self._image_centered='array_center'                     # one of 'array_center', 'pixel', 'corner'
                                                                # This records where the coordinate origin is
                                                                # in image planes, and depends on how the imageg
                                                                # plane was produced (e.g. FFT implies pixel)
        "Are FT'ed image planes centered on a pixel or on a corner between pixels? "
        self.wavefront = np.ones((npix,npix), dtype=dtype)   # the actual complex wavefront array
        self.ispadded = False                               # is the wavefront padded for oversampling?
        self.history=[]
        "List of strings giving a descriptive history of actions performed on the wavefront. Saved to FITS headers."
        self.history.append("Created wavefront: wavelen=%g m, diam=%f m" %(self.wavelength, self.diam))
        self.history.append(" using array size %s" % (self.wavefront.shape,) )
        self.location='Entrance'
        "A descriptive string for where a wavefront is instantaneously located (e.g. 'before occulter'). Used mostly for titling displayed plots."

    def __str__(self):
        # TODO add switches for image/pupil planes
        return """Wavefront:
        wavelength = %f microns
        shape = (%d,%d)
        sampling = %f meters/pixel
        """ % (self.wavelength/1e-6, self.wavefront.shape[0], self.wavefront.shape[1], self.pixelscale )

    def copy(self):
        "Return a copy of the wavefront as a different object."
        return copy.deepcopy(self)

    def normalize(self):
        "Set this wavefront's total intensity to 1 "
        #print "Wavefront normalized"
        self.wavefront /= np.sqrt(self.totalIntensity)

    def __imul__(self, optic):
        "Multiply a Wavefront by an OpticalElement or scalar"
        if isinstance(optic,Rotation):
            return self # a rotation doesn't actually affect the wavefront via multiplication,
                        # but instead via forcing a call to rotate() elsewhere...
        elif (isinstance(optic,float)) or isinstance(optic,int):
            self.wavefront *= optic # it's just a scalar
            self.history.append("Multiplied WF by scalar value "+str(optic))
            return self


        if (not isinstance(optic, OpticalElement)) :
            raise ValueError('Wavefronts can only be *= multiplied by OpticalElements or scalar values')

        if isinstance(optic,Detector):
            # detectors don't modify a wavefront.
            return self

        phasor = optic.getPhasor(self)

        if not np.isscalar(phasor) and phasor.size>1:  # actually isscalar() does not handle the case of a 1-element array properly
            #print self.wavefront.shape, phasor.shape
            assert self.wavefront.shape == phasor.shape

        self.wavefront *= phasor
        msg =  "  Multiplied WF by phasor for "+str(optic)
        _log.debug(msg)
        self.history.append(msg)
        self.location='after '+optic.name
        return self

    def __mul__(self, optic):
        """ Multiply a wavefront by an OpticalElement or scalar """
        new = self.copy()
        new *= optic
        return new
    __rmul__ = __mul__  # either way works.


    def __iadd__(self,wave):
        "Add another wavefront to this one"
        if not isinstance(wave,Wavefront):
            raise ValueError('Wavefronts can only be summed with other Wavefronts')

        if not self.wavefront.shape[0] == wave.wavefront.shape[0]:
            raise ValueError('Wavefronts can only be added if they have the same size and shape')

        self.wavefront += wave.wavefront
        self.history.append("Summed with another wavefront!")
        return self

    def __add__(self,wave):
        new = self.copy()
        new += wave
        return new

    def asFITS(self, what='intensity', includepadding=False, **kwargs):
        """ Return a wavefront as a pyFITS HDUList object

        Parameters
        -----------
        what : string
            what kind of data to write. Must be one of 'parts', 'intensity', 'complex'.
            The default is to write a file containing intensity.
        includepadding : bool
            include any "padding" region, if present, in the returned FITS file?

        """
        # make copies in case we need to unpad - don't want to mess up actual wavefront data in memory
        # FIXME this is somewhat inefficient but easiest to code for now
        intens = self.intensity.copy() 
        amp = self.amplitude.copy()
        phase = self.phase.copy()
        wave = self.wavefront.copy()

        if self.planetype==_PUPIL and self.ispadded and not includepadding :
            intens = removePadding(intens,self.oversample)
            phase = removePadding(phase,self.oversample)
            amp = removePadding(amp,self.oversample)
            wave = removePadding(wave,self.oversample)


        if what.lower() =='all':
            outarr = np.zeros((3,intens.shape[0], intens.shape[1]))
            outarr[0,:,:] = intens
            outarr[1,:,:] = amp
            outarr[2,:,:] = phase
            outFITS = fits.HDUList(fits.PrimaryHDU(outarr))
            outFITS[0].header.update('PLANE1', 'Wavefront Intensity')
            outFITS[0].header.update('PLANE2', 'Wavefront Amplitude')
            outFITS[0].header.update('PLANE3', 'Wavefront Phase')
        elif what.lower() =='parts':
            outarr = np.zeros((2,amp.shape[0], amp.shape[1]))
            outarr[0,:,:] = amp
            outarr[1,:,:] = phase
            outFITS = fits.HDUList(fits.PrimaryHDU(outarr))
            outFITS[0].header.update('PLANE1', 'Wavefront Amplitude')
            outFITS[0].header.update('PLANE2', 'Wavefront Phase')
        elif what.lower() =='intensity':
            outFITS = fits.HDUList(fits.PrimaryHDU(intens))
            outFITS[0].header.update('PLANE1', 'Wavefront Intensity')
        elif what.lower() =='phase':
            outFITS = fits.HDUList(fits.PrimaryHDU(phase))
            outFITS[0].header.update('PLANE1', 'Phase')
        elif what.lower()  == 'complex':
            outFITS = fits.HDUList(fits.PrimaryHDU(wave))
            outFITS[0].header.update('PLANE1', 'Wavefront Complex Phasor ')




        outFITS[0].header.update('WAVELEN', self.wavelength, 'Wavelength in meters')
        outFITS[0].header.update('DIFFLMT', self.wavelength/self.diam*206265., 'Diffraction limit lambda/D in arcsec')
        outFITS[0].header.update('OVERSAMP', self.oversample, 'Oversampling factor for FFTs in computation')
        outFITS[0].header.update('DET_SAMP', self.oversample, 'Oversampling factor for MFT to detector plane')
        if self.planetype ==_IMAGE:
            outFITS[0].header.update('PIXELSCL', self.pixelscale, 'Scale in arcsec/pix (after oversampling)')
            if np.isscalar(self.fov):
                outFITS[0].header.update('FOV', self.fov, 'Field of view in arcsec (full array)')
            else:
                outFITS[0].header.update('FOV_X', self.fov[1], 'Field of view in arcsec (full array), X direction')
                outFITS[0].header.update('FOV_Y', self.fov[0], 'Field of view in arcsec (full array), Y direction')
        else:
            outFITS[0].header.update('PIXELSCL', self.pixelscale, 'Pixel scale in meters/pixel')
            outFITS[0].header.update('DIAM', self.diam, 'Pupil diameter in meters (not incl padding)')

        for h in self.history: outFITS[0].header.add_history(h)

        return outFITS

    def writeto(self,filename, clobber=True, **kwargs):
        """Write a wavefront to a FITS file.

        Parameters
        -----------
        filename : string
            filename to use
        what : string
            what to write. Must be one of 'parts', 'intensity', 'complex'
        clobber : bool, optional
            overwhat existing? default is True

        Returns
        -------
        outfile: file on disk
            The output is written to disk.

        """
        self.asFITS(**kwargs).writeto(filename, clobber=clobber)
        _log.info("  Wavefront saved to %s" % filename)

    def display(self,what='intensity', nrows=1,row=1,showpadding=False,imagecrop=None, colorbar=False, crosshairs=True, ax=None, title=None,vmin=1e-8,vmax=1e0):
        """Display wavefront on screen

        Parameters
        ----------
        what : string
           What to display. Must be one of {intensity, phase, best}.
           'Best' implies to display the phase if there is nonzero OPD, or else
           display the intensity for a perfect pupil.

        nrows : int
            Number of rows to display in current figure (used for showing steps in a calculation)
        row : int
            Which row to display this one in?
        imagecrop : float, optional
            For image planes, set the maximum # of arcseconds to display. Default is 5, so
            only the innermost 5x5 arcsecond region will be shown.
        showpadding : bool, optional
            Show the entire padded arrays, or just the good parts? Default is False
        colorbar : bool
            Display colorbar
        ax : matplotlib Axes
            axes to display into

        Returns
        -------
        figure : matplotlib figure
            The current figure is modified.


        """
        if imagecrop is None: imagecrop = settings.default_image_display_fov()

        intens = self.intensity.copy()
        phase  = self.phase.copy()
        phase[np.where(intens ==0)] = np.nan
        amp    = self.amplitude

        if self.planetype==_PUPIL and self.ispadded and not showpadding :
            intens = removePadding(intens,self.oversample)
            phase = removePadding(phase,self.oversample)
            amp = removePadding(amp,self.oversample)


        # extent specifications need to include the *full* data region, including the half pixel on either
        # side outside of the pixel center coordinates.  And remember to swap Y and X.  Recall that for matplotlib,
        #    extent = [xmin, xmax, ymin, ymax]
        # in this case those are coordinates in units of pixels. Recall that we define pixel coordinates to be
        # at the *center* of the pixel, so we compute here the coordinates at the outside of those pixels. 
        # This is needed to get the coordinates right when displaying very small arrays

        extent = np.array([-0.5 ,intens.shape[1]-1+0.5, -0.5,intens.shape[0]-1+0.5]) * self.pixelscale
        if self.planetype == _PUPIL:
            # For pupils, we just let the 0 point be that of the array, off to the side of the actual clear aperture
            unit = "m"
        else:
            # for image planes, we make coordinates relative to center.
            # image plane coordinates depend slightly on whether the optical center is at a 
            # pixel-center or the corner between 4 pixels...
            if self._image_centered == 'array_center' or self._image_centered=='corner':
                cenx = (intens.shape[1]-1)/2.
                ceny = (intens.shape[0]-1)/2.
            elif self._image_centered == 'pixel':
                cenx = (intens.shape[1])/2.
                ceny = (intens.shape[0])/2.

            extent -= np.asarray([cenx, cenx, ceny, ceny])*self.pixelscale
            halffov_x = intens.shape[1]/2.*self.pixelscale #for use later
            halffov_y = intens.shape[0]/2.*self.pixelscale #for use later
            unit="arcsec"

        # implement semi-intellegent selection of what to display, if the user wants
        if what =='best':
            if self.planetype ==_IMAGE:
                what = 'intensity' # always show intensity for image planes
            elif phase[np.where(np.isfinite(phase))].sum() == 0:
                what = 'intensity' # for perfect pupils
            elif int(row) > 2: what='intensity'  # show intensity for coronagraphic downstream propagation.
            else: what='phase' # for aberrated pupils

        # compute plot parameters for the subplot grid
        nc = int(np.ceil(np.sqrt(nrows)))
        nr = int(np.ceil(float(nrows)/nc))
        if nrows - nc*(nc-1) == 1: # avoid just one alone on a row by itself...
            nr -= 1
            nc += 1

        # now display the chosen selection..
        if what == 'intensity':
            if self.planetype == _PUPIL:
                norm=matplotlib.colors.Normalize(vmin=0)
                cmap = matplotlib.cm.gray
                cmap.set_bad('0.0')
            else:
                norm=LogNorm(vmin=vmin,vmax=vmax)
                cmap = matplotlib.cm.jet
                cmap.set_bad(cmap(0))

            if ax is None:
                ax = plt.subplot(nr,nc,int(row))

            utils.imshow_with_mouseover(intens, ax=ax, extent=extent, norm=norm, cmap=cmap)
            if title is None:
                title = "Intensity "+self.location
                title = title.replace('after', 'after\n')
                title = title.replace('before', 'before\n')
            plt.title(title)
            plt.xlabel(unit)
            if colorbar: plt.colorbar(ax.images[0], orientation='vertical', shrink=0.8)

            if self.planetype ==_IMAGE:
                if crosshairs:
                    plt.axhline(0,ls=":", color='k')
                    plt.axvline(0,ls=":", color='k')
                imsize_x = min( (imagecrop, halffov_x))
                imsize_y = min( (imagecrop, halffov_y))
                ax.set_xbound(-imsize_x, imsize_x)
                ax.set_ybound(-imsize_y, imsize_y)
        elif what =='phase':
            # Display phase in waves.
            cmap = matplotlib.cm.jet
            cmap.set_bad('0.3')
            norm=matplotlib.colors.Normalize(vmin=-0.25,vmax=0.25)
            if ax is None:
                ax = plt.subplot(nr,nc,int(row))
            utils.imshow_with_mouseover(phase/(np.pi*2), ax=ax, extent=extent, norm=norm, cmap=cmap)
            if title is None:
                title= "Phase "+self.location
            plt.title(title)
            plt.xlabel(unit)
            if colorbar: plt.colorbar(ax.images[0], orientation='vertical', shrink=0.8)


        else:
            if ax is None:
                ax = plt.subplot(nr,nc,int(row))
            cmap = matplotlib.cm.gray
            plt.subplot(nrows,2,(row*2)-1)
            plt.imshow(amp,extent=extent,cmap=cmap)
            plt.title("Wavefront amplitude")
            plt.ylabel(unit)
            if colorbar: plt.colorbar(orientation='vertical',shrink=0.8)

            plt.subplot(nrows,2,row*2)
            plt.imshow(phase,extent=extent, cmap=cmap)
            if colorbar: plt.colorbar(orientation='vertical',shrink=0.8)

            plt.ylabel(unit)
            plt.title("Wavefront phase [radians]")

        ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(5))
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(5))


        plt.draw()

    # add convenient properties for intensity, phase, amplitude, total_flux
    @property
    def amplitude(self):
        "Electric field amplitude of the wavefront "
        return np.abs(self.wavefront)

    @property
    def intensity(self):
        "Electric field intensity of the wavefront (i.e. field amplitude squared)"
        return np.abs(self.wavefront)**2

    @property
    def phase(self):
        "Phase of the wavefront, in radians"
        return np.angle(self.wavefront)

    @property
    def shape(self):
        """ Shape of the wavefront array"""
        return self.wavefront.shape

    @property
    def totalIntensity(self):
        "Integrated intensity over the entire spatial/angular extent of the wavefront"
        return self.intensity.sum()

    # methods for wavefront propagation:
    def propagateTo(self, optic):
        """Propagates a wavefront object to the next optic in the list.
        Modifies this wavefront object itself.

        Parameters
        -----------
        optic : OpticalElement
            The optic to propagate to. Used for determining the appropriate optical plane.
        """
        if self.planetype == optic.planetype:
            _log.debug("  Wavefront and optic %s already at same plane type, no propagation needed." % optic.name)
            return
        else:
            msg = "  Propagating wavefront to %s. " % str(optic)
            _log.debug(msg)
            self.history.append(msg)

        #_log.debug('==== at %s, to %s' % (typestrs[self.planetype], typestrs[optic.planetype]))
        if optic.planetype == _ROTATION:     # rotate
            self.rotate(optic.angle)
            self.location='after '+optic.name
        elif optic.planetype == _DETECTOR and self.planetype ==_PUPIL:    # MFT pupil to detector
            self._propagateMFT(optic)
            self.location='before '+optic.name
        elif optic.planetype == _PUPIL and self.planetype ==_IMAGE and self._last_transform_type =='MFT': # inverse MFT detector to pupil
            self._propagateMFTinverse(optic)
            self.location='before '+optic.name
        elif self.planetype==_IMAGE and optic.planetype == _DETECTOR:
            raise NotImplemented('image plane directly to detector propagation (resampling!) not implemented yet')
        else:
            self._propagateFFT(optic)           # FFT pupil to image or image to pupil
            self.location='before '+optic.name

    def _propagateFFT(self, optic):
        """ Propagate from pupil to image or vice versa using a padded FFT 

        Parameters
        -----------
        optic : OpticalElement
            The optic to propagate to. Used for determining the appropriate optical plane.

        """


        if self.oversample > 1 and not self.ispadded: #add padding for oversampling, if necessary
            assert self.oversample == optic.oversample
            self.wavefront = padToOversample(self.wavefront, self.oversample)
            self.ispadded = True
            if optic.verbose: _log.debug("    Padded WF array for oversampling by %dx" % self.oversample)
            self.history.append("    Padded WF array for oversampling by %dx" % self.oversample)

        # Set up for computation - figure out direction & normalization
        if self.planetype == _PUPIL and optic.planetype == _IMAGE:
            FFT_direction = 'forward'
            normalization_factor = 1./ self.wavefront.shape[0] # correct for numpy fft
            numpy_fft = np.fft.fft2
            numpy_fftshift = np.fft.fftshift
            #(pre-)update state:
            self.planetype=_IMAGE
            self.pixelscale = self.wavelength/ self.diam / self.oversample * _RADIANStoARCSEC
            self.fov = self.wavefront.shape[0] * self.pixelscale
            self.history.append('   FFT %s,  to _IMAGE  scale=%f' %(self.wavefront.shape, self.pixelscale))

        elif self.planetype == _IMAGE and optic.planetype ==_PUPIL:
            FFT_direction = 'backward'
            if settings.use_fftw(): #_USE_FFTW:
                normalization_factor =  1./self.wavefront.shape[0] # correct for FFTW3 FFT
                # TODO check this for pyfftw
            else:
                normalization_factor =  self.wavefront.shape[0] # correct for numpy fft
            numpy_fft = np.fft.ifft2
            numpy_ifftshift = np.fft.ifftshift
            #(pre-)update state:
            self.planetype=_PUPIL
            self.pixelscale = self.diam *self.oversample / self.wavefront.shape[0]
            self.history.append('   FFT %s,  to _PUPIL scale=%f' %(self.wavefront.shape, self.pixelscale))


        # do FFT
        if settings.enable_flux_tests(): _log.debug("\tPre-FFT total intensity: "+str(self.totalIntensity))
        if settings.enable_speed_tests(): t0 = time.time()

        if FFT_direction =='backward': self.wavefront = numpy_ifftshift(self.wavefront)

        if settings.use_fftw(): # _USE_FFTW:
            _log.debug("using FFTW FFT  of {0} array".format(str(self.wavefront.shape)))
            # Benchmarking on a Mac Pro (8 cores) indicated that the fastest performance comes from
            # in-place FFTs, and that it is safe to ignore byte alignment issues for these arrays
            # (indeed, even beneficial in many cases) contrary to the suggestion of the FFTW docs
            # which say that aligning arrays helps. Not sure why, but it's true!
            # See the discussion of FFTs in the documentation.
            #wfold = self.copy()
            if 0:  # old code for fftw3
                if (self.wavefront.shape, FFT_direction) not in _FFTW_INIT.keys():
                    # The first time you run FFTW to transform a given size, it does a speed test to determine optimal algorithm
                    # that is destructive to your chosen array. So only do that test on a copy, not the real array:
                    _log.info("Evaluating FFT optimal algorithm for %s, direction=%s" % (str(self.wavefront.shape), FFT_direction))
                    fftplan = fftw3.Plan(self.wavefront.copy(), None, nthreads = multiprocessing.cpu_count(),direction=FFT_direction, flags=_FFTW_FLAGS)
                    _FFTW_INIT[(self.wavefront.shape, FFT_direction)] = True

                fftplan = fftw3.Plan(self.wavefront, None, nthreads = multiprocessing.cpu_count(),direction=FFT_direction, flags=_FFTW_FLAGS)
                fftplan.execute() # execute the plan
                    #print "After  FFTW Flux 2: %f" % (abs(outarr)**2).sum()
                # due to FFTW normalization convention, must divide by number of pixels per side.
                    #print "After  FFTW Flux 1: %f" % (self.totalIntensity)
            else:  # new code for pyfftw
                import pyfftw
                if (self.wavefront.shape, FFT_direction) not in _FFTW_INIT.keys():
                    # The first time you run FFTW to transform a given size, it does a speed test to determine optimal algorithm
                    # that is destructive to your chosen array. So only do that test on a copy, not the real array:
                    _log.info("Evaluating PyFFT optimal algorithm for %s, direction=%s" % (str(self.wavefront.shape), FFT_direction))

                    pyfftw.interfaces.cache.enable()
                    pyfftw.interfaces.cache.set_keepalive_time(30)

                    #if byte_align: test_array = pyfftw.n_byte_align_empty( self.wavefront.shape, 16, dtype=dtype)
                    test_array = np.zeros(self.wavefront.shape)
                    test_array = pyfftw.interfaces.numpy_fft.fft2(test_array, overwrite_input=True, planner_effort='FFTW_MEASURE', threads=multiprocessing.cpu_count())

                    _FFTW_INIT[(self.wavefront.shape, FFT_direction)] = True

                self.wavefront = pyfftw.interfaces.numpy_fft.fft2(self.wavefront, overwrite_input=True, planner_effort='FFTW_MEASURE', threads=multiprocessing.cpu_count())




        else:
            _log.debug("using numpy FFT")
            self.wavefront = numpy_fft(self.wavefront)

        if FFT_direction == 'forward':
            self.wavefront = numpy_fftshift(self.wavefront)
            # FFT produces pixel-centered images by default, unless the _image_centered param has already been set by an FQPM_FFT_aligner class
            if self._image_centered != 'corner': self._image_centered = 'pixel'
        self.wavefront = self.wavefront *normalization_factor
        self._last_transform_type = 'FFT'

        if settings.enable_speed_tests():
            t1 = time.time()
            _log.debug("\tTIME %f s\t for the FFT" % (t1-t0))

        if settings.enable_flux_tests(): _log.debug("\tPost-FFT total intensity: "+str(self.totalIntensity))



    def _propagateMFT(self, det):
        """ Compute from pupil to an image using the Soummer et al. 2007 MFT algorithm
        
        Parameters
        -----------
        det : OpticalElement, must be of type DETECTOR
            The target optical plane to propagate to."""

        assert self.planetype == _PUPIL
        assert det.planetype == _DETECTOR

        if self.ispadded:
            #pupil plane is padded - trim that out since it's not needed
            self.wavefront = removePadding(self.wavefront, self.oversample)
            self.ispadded = False
        self._preMFT_pupil_shape =self.wavefront.shape  #save for possible inverseMFT
        self._preMFT_pupil_pixelscale = self.pixelscale #save for possible inverseMFT


        # the arguments for the matrixDFT are
        # - wavefront (assumed to fill the input array)
        # - focal plane size in lambda/D units
        # - number of pixels on a side in focal plane array.

        lamD = self.wavelength / self.diam * _RADIANStoARCSEC

        det_fov_lamD = det.fov_arcsec / lamD
        det_calc_size_pixels = det.fov_pixels * det.oversample

        mft = MatrixFourierTransform(centering='ADJUSTIBLE', verbose=False)
        if not np.isscalar(det_fov_lamD): #hasattr(det_fov_lamD,'__len__'):
            msg= '    Propagating w/ MFT: %.4f"/pix     fov=[%.3f,%.3f] lam/D    npix=%d x %d' %  (det.pixelscale/det.oversample, det_fov_lamD[0], det_fov_lamD[1], det_calc_size_pixels[0], det_calc_size_pixels[1])
        else:
            msg= '    Propagating w/ MFT: %.4f"/pix     fov=%.3f lam/D    npix=%d' %  (det.pixelscale/det.oversample, det_fov_lamD, det_calc_size_pixels)
        _log.debug(msg)
        self.history.append(msg)
        det_offset = det.det_offset if hasattr(det, 'det_offset') else (0,0)


        _log.debug('      MFT method = '+mft.centering)

        # det_offset controls how to shift the PSF.
        # it gives the coordinates (X, Y) relative to the exact center of the array
        # for the location of the phase center of a converging perfect spherical wavefront.
        # This is where a perfect PSF would be centered. Of course any tilts, comas, etc, from the OPD
        # will probably shift it off elsewhere for an entirely different reason, too.
        self.wavefront = mft.perform(self.wavefront, det_fov_lamD, det_calc_size_pixels, offset=det_offset)
        _log.debug("     Result wavefront: at={0} shape={1} intensity={2:.3g}".format(self.location, str(self.shape), self.totalIntensity))
        self._last_transform_type = 'MFT'

        self.planetype=_IMAGE
        self.fov = det.fov_arcsec
        self.pixelscale = det.fov_arcsec / det_calc_size_pixels

        if not np.isscalar(self.pixelscale):
            # check for rectangular arrays
            if self.pixelscale[0] == self.pixelscale[1]: 
                self.pixelscale = self.pixelscale[0]  # we're in a rectangular array with same pixel scale in both directions, so treat pixelscale as a scalar
            else:
                raise NotImplementedError('Different pixel scales in X and Y directions (i.e. non-square pixels) not yet supported.') 

    def _propagateMFTinverse(self, pupil, pupil_npix=None):
        """ Compute from an image to a pupil using the Soummer et al. 2007 MFT algorithm
        This allows transformation back from an arbitrarily-sampled 'detector' plane to a pupil. 

        This is only used inside the semi-analytic coronagraphy algorithm.

        """

        assert self.planetype == _IMAGE
        assert pupil.planetype == _PUPIL

        # the arguments for the matrixDFT are
        # - wavefront (assumed to fill the input array)
        # - focal plane size in lambda/D units
        # - number of pixels on a side in focal plane array.

        lamD = self.wavelength / self.diam * _RADIANStoARCSEC
        #print "lam/D = %f arcsec" % lamD

        det_fov_lamD = self.fov / lamD
        #det_calc_size_pixels = det.fov_pixels * det.oversample

        # try to transform to whatever the intrinsic scale of the next pupil is. 
        # but if this ends up being a scalar (meaning it is an AnalyticOptic) then
        # just go back to our own prior shape and pixel scale.
        if pupil_npix == None:
            if pupil.shape is not None and pupil.shape[0] != 1:
                pupil_npix = pupil.shape[0]
            else:
                pupil_npix = self._preMFT_pupil_shape[0]

        mft = MatrixFourierTransform(centering='ADJUSTIBLE', verbose=False)
        if not np.isscalar(det_fov_lamD): #hasattr(det_fov_lamD,'__len__'):
            msg= '    Propagating w/ InvMFT: %.4f"/pix     fov=[%.3f,%.3f] lam/D    npix=%d x %d' %  (self.pixelscale[0], det_fov_lamD[0], det_fov_lamD[1], pupil_npix, pupil_npix)
        else:
            msg= '    Propagating w/ InvMFT: %.4f"/pix     fov=%.3f lam/D    pupil npix=%d' %  (self.pixelscale, det_fov_lamD, pupil_npix)
        _log.debug(msg)
        self.history.append(msg)
        det_offset = (0,0)  # det_offset not supported for InvMFT

        self.wavefront = mft.inverse(self.wavefront, det_fov_lamD, pupil_npix)
        self._last_transform_type = 'InvMFT'

        self.planetype=_PUPIL
        self.pixelscale = self.diam / self.wavefront.shape[0]


    def tilt(self, Xangle=0.0, Yangle=0.0):
        """ Tilt a wavefront in X and Y.

        Recall from Fourier optics (although this is straightforwardly rederivable by drawing triangles)
        that for a wavefront tilted by some angle theta in radians, that a point r meters from the center of
        the pupil has:

            extra_pathlength = sin(theta) * r
            extra_waves = extra_pathlength/ wavelength = r * sin(theta) / wavelength

        So we calculate the U and V arrays (corresponding to r for the pupil, in meters from the center)
        and then multiply by the appropriate trig factors for the angle.

        The sign convention is chosen such that positive Yangle tilts move the star upwards in the
        array at the focal plane. (This is sort of an inverse of what physically happens in the propagation
        to or through focus, but we're ignoring that here and trying to just work in sky coords)

        Parameters
        ----------
        Xangle, Yangle : float
            tilt angles, specified in arcseconds

        """
        if self.planetype==_IMAGE:
            raise NotImplementedError("Are you sure you want to tilt a wavefront in an _IMAGE plane?")

        if np.abs(Xangle) > 0 or np.abs(Yangle)>0:
            xangle_rad = Xangle * (np.pi/180/60/60)
            yangle_rad = Yangle * (np.pi/180/60/60)

            npix = self.wavefront.shape[0]
            V, U = np.indices(self.wavefront.shape, dtype=float)
            V -= (npix-1)/2.0
            V *= self.pixelscale
            U -= (npix-1)/2.0
            U *= self.pixelscale

            tiltphasor = np.exp( 2j*np.pi * (U * xangle_rad + V * yangle_rad)/self.wavelength)

        else:
            _log.warn("Wavefront.tilt() called, but requested tilt was zero. No change.")
            tiltphasor = 1.

        #Compute the tilt of the wavefront required to shift it by some amount in the image plane.




        self.wavefront *= tiltphasor
        self.history.append("Tilted wavefront")

    def rotate(self, angle=0.0):
        """Rotate a wavefront by some amount

        Parameters
        ----------
        angle : float
            Angle to rotate, in degrees counterclockwise.

        """
        #self.wavefront = scipy.ndimage.interpolation.rotate(self.wavefront, angle, reshape=False)
        # Huh, the ndimage rotate function does not work for complex numbers. That's weird.
        # so let's treat the real and imaginary parts individually
        # FIXME TODO or would it be better to do this on the amplitude and phase?
        rot_real = scipy.ndimage.interpolation.rotate(self.wavefront.real, angle, reshape=False)
        rot_imag = scipy.ndimage.interpolation.rotate(self.wavefront.imag, angle, reshape=False)
        self.wavefront = rot_real + 1.j*rot_imag

        self.history.append('Rotated by %f degrees, CCW' %(angle))


    def coordinates(self):
        """ Return Y, X coordinates for this wavefront, in the manner of numpy.indices()

        This function knows about the offset resulting from FFTs. Use it whenever computing anything
        measures in wavefront coordinates.

        Returns
        -------
        Y, X :  array_like
            Wavefront coordinates in either meters or arcseconds for pupil and image, respectively

        """
        y, x = np.indices(self.shape, dtype=float)

        # in most cases, the x and y values are centered around the exact center of the array.
        # This is not true in general for FFT-produced image planes where the center is in the
        # middle of one single pixel (the 0th-order term of the FFT), even though that means that the
        # PSF center is slightly offset from the array center.
        # On the other hand, if we used the FQPM FFT Aligner optic, then that forces the PSF center to
        # the exact center of an array.
        if self.planetype == _PUPIL:
            y-= (self.shape[0]-1)/2.
            x-= (self.shape[1]-1)/2.
        elif self.planetype == _IMAGE:
            # The following are just relevant for the FFT-created images, not for the Detector MFT image at the end.
            if self._last_transform_type == 'FFT':
                # FFT array sizes will always be even, right?
                if self._image_centered=='pixel':  # so this goes to an integer pixel
                    y-= (self.shape[0])/2.
                    x-= (self.shape[1])/2.
                elif self._image_centered=='array_center' or self._image_centered=='corner':  # and this goes to a pixel center
                    y-= (self.shape[0]-1)/2.
                    x-= (self.shape[1]-1)/2.
            else:
                # MFT produced images are always exactly centered.
                y-= (self.shape[0]-1)/2.
                x-= (self.shape[1]-1)/2.


        if not np.isscalar(self.pixelscale): #hasattr(self.pixelscale,'__len__'):
            xscale=self.pixelscale[0]
            yscale=self.pixelscale[1]
        else:
            xscale=self.pixelscale
            yscale=self.pixelscale

        #x *= xscale
        #y *= yscale
        return y*yscale, x*xscale


#------ Optical Element Classes ------

class OpticalElement():
    """ Base class for all optical elements, whether from FITS files or analytic functions. 

    If instantiated on its own, this just produces a null optical element (empty space, 
    i.e. an identity function on transmitted wavefronts.) Use one of the many subclasses to
    create a nontrivial optic.

    The OpticalElement class follows the behavoior of the Wavefront class, using units
    of meters/pixel in pupil space and arcsec/pixel in image space.

    The internal implementation of this class represents an optic with an array
    for the electric field amplitude transmissivity (or reflectivity), plus an
    array for the optical path difference in units of meters. This
    representation was chosen since most typical optics of interest will have
    wavefront error properties that are independent of wavelength. Subclasses
    particularly the AnalyticOpticalElements extend this paradigm with optics
    that have wavelength-dependent properties.

    The getPhasor() function is used to obtain the complex phasor for any desired 
    wavelength based on the amplitude and opd arrays. 

    Parameters
    ----------
    name : string
        descriptive name for optic
    verbose : bool
        whether to print stuff while computing
    planetype : int
        either poppy._IMAGE or poppy._PUPIL
    oversample : int
        how much to oversample beyond Nyquist.

    """
    #pixelscale = None
    #"float attribute. Pixelscale in arcsec or meters per pixel. Will be 'None' for null or analytic optics."


    def __init__(self, name="unnamed optic", verbose=True, planetype=None, oversample=1, opdunits="meters"):

        self.name = name
        """ string. Descriptive Name of this optic"""
        self.verbose=verbose

        self.planetype = planetype      # pupil or image
        self.oversample = oversample    # oversampling factor, none by default
        self.ispadded = False           # are we padded w/ zeros for oversampling the FFT?
        self._suppress_display=False    # should we avoid displaying this optic on screen? (useful for 'virtual' optics like FQPM aligner)

        #_log.warn("Creating a null optical element. Are you sure that's what you want to do?")
        self.amplitude = np.asarray([1.])
        self.opd = np.asarray([0.])
        self.pixelscale = None
 
    def getPhasor(self,wave):
        """ Compute a complex phasor from an OPD, given a wavelength.

        The returned value should be the complex phasor array as appropriate for
        multiplying by the wavefront amplitude. 

        Parameters
        ----------
        wave : float or obj
            either a scalar wavelength or a Wavefront object

        """
        #_log.info("Pixelscales for %s: wave %f, optic  %f" % (self.name, wave.pixelscale, self.pixelscale))

        if isinstance(wave, Wavefront):
            wavelength=wave.wavelength
        else:
            wavelength=wave
        scale = 2. * np.pi / wavelength

        # set the self.phasor attribute:
        # first check whether we need to interpolate to do this.
        float_tolerance = 0.001  #how big of a relative scale mismatch before resampling?
        if self.pixelscale is not None and hasattr(wave,'pixelscale') and abs(wave.pixelscale -self.pixelscale)/self.pixelscale >= float_tolerance:
            _log.debug("Pixelscales: wave %f, optic %f" % (wave.pixelscale, self.pixelscale))

            raise ValueError("Non-matching pixel scale for wavefront and optic! Need to add interpolation / rescaling ")
            if self.has_attr('_resampled_scale') and abs(self._resampled_scale-wave.pixelscale)/self._resampled_scale >= float_tolerance:
                # we already did this same resampling, so just re-use it!
                self.phasor = self._resampled_amplitude * np.exp (1.j * self._resampled_opd * scale)
            else:
                raise NotImplementedError("Need to implement resampling.")

        else:
            # compute the phasor directly, without any need to rescale.
            self.phasor = self.amplitude * np.exp (1.j * self.opd * scale)



        # check whether we need to pad before returning or not.
        # note: do not pad the phasor if it's just a scalar!
        if self.planetype == _PUPIL and wave.ispadded and self.phasor.size !=1:
            # old version: pad to a fixed oversampling. All FITS arrays in an OpticalSystem must be the same size
            #return padToOversample(self.phasor, wave.oversample)

            # new version: pad to match the wavefront sampling, from whatever sized array we started with. Allows more
            # flexibility for differently sized FITS arrays, so long as they all have the same pixel scale as checked above!
            return padToSize(self.phasor, wave.shape)
        else:
            return self.phasor

    def display(self, nrows=1, row=1, what='intensity', crosshairs=True, ax=None, colorbar=True, colorbar_orientation=None, title=None, opd_vmax=0.5e-6):
        """Display plots showing an optic's transmission and OPD.

        Parameters
        ----------
        what : str
            What to display: 'intensity', 'amplitude', 'phase', or 'both' (meaning intensity + phase)
        ax : matplotlib.Axes instance 
            Axes to display into
        nrows, row : integers
            # of rows and row index for subplot display
        crosshairs : bool
            Display crosshairs indicating the center?
        colorbar : bool
            Show colorbar?
        colorbar_orientation : bool
            Desired orientation, horizontal or vertical?
            Default is horizontal if only 1 row of plots, else vertical
        opd_vmax : float
            Max value for OPD image display, in meters.
        title : string
            Plot label


        """
        if colorbar_orientation is None:
            colorbar_orientation= "horizontal" if nrows == 1 else 'vertical'

        _log.debug('colorbar_orientation = '+colorbar_orientation)
        cmap_amp = matplotlib.cm.gray
        cmap_amp.set_bad('0.0')
        cmap_opd = matplotlib.cm.jet
        cmap_opd.set_bad('0.3')
        norm_amp=matplotlib.colors.Normalize(vmin=0, vmax=1)
        norm_opd=matplotlib.colors.Normalize(vmin=-opd_vmax, vmax=opd_vmax)

        units = "[meters]" if self.planetype == _PUPIL else "[arcsec]"
        if nrows > 1: units = self.name+"\n"+units


        if self.pixelscale is not None:
            halfsize = self.pixelscale*self.amplitude.shape[0]/2
            _log.debug("Display pixel scale = %.3f " % self.pixelscale)
        else:
            _log.debug("No defined pixel scale - this must be an analytic optic")
            halfsize=1.0
        #extent = [0,pixelscale*self.amplitude.shape[0], 0,pixelscale*self.amplitude.shape[1]]
        extent = [-halfsize, halfsize, -halfsize, halfsize]


        #ampl = np.ma.masked_equal(self.amplitude, 0)
        ampl = self.amplitude
        #opd= np.ma.masked_array(self.opd, mask=(self.amplitude ==0))
        opd = self.opd.copy()
        opd[np.where(self.amplitude ==0)] = np.nan

        if what =='both':
            # recursion!
            if ax is None:
                ax = plt.subplot(nrows, 2, row*2-1)
            self.display(what='intensity', ax=ax, crosshairs=crosshairs, colorbar=colorbar, nrows=nrows)
            ax2 = plt.subplot(nrows, 2, row*2)
            self.display(what='phase', ax=ax2, crosshairs=crosshairs, colorbar=colorbar, nrows=nrows)
            return
        elif what=='amplitude':
            plot_array = ampl
            title = 'Transmissivity'
            cb_label = 'Fraction'
            cb_values = [0,0.25, 0.5, 0.75, 1.0]
            cmap = cmap_amp
            norm = norm_amp
        elif what=='intensity':
            plot_array = ampl**2
            title = "Transmittance"
            cb_label = 'Fraction'
            cb_values = [0,0.25, 0.5, 0.75, 1.0]
            cmap = cmap_amp
            norm = norm_amp
        elif what =='phase':
            plot_array = opd
            title = "OPD"
            cb_label = 'meters'
            cb_values = np.array([-1, -0.5, 0, 0.5, 1])*opd_vmax
            cmap = cmap_opd
            norm = norm_opd
        
        # now we plot whichever was chosen...
        if ax is None:
            if nrows > 1:
                ax = plt.subplot(nrows, 2, row*2-1)
            else: ax = plt.subplot(111)
        utils.imshow_with_mouseover(plot_array, ax=ax, extent=extent, cmap=cmap, norm=norm)
        if nrows == 1:
            plt.title(title+" for "+self.name)
        plt.ylabel(units)
        ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=4, integer=True))
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=4, integer=True))
        if colorbar: 
            cb = plt.colorbar(ax.images[0], orientation=colorbar_orientation, ticks=cb_values)
            cb.set_label(cb_label)
        if crosshairs:
            ax.axhline(0,ls=":", color='k')
            ax.axvline(0,ls=":", color='k')


 
#        if what=='intensity' or what=='both':
#            # Note that the ampl variable is the *amplitude* transmissivity. 
#            # What we want to display is what happens to the wavefront's intensity
#            # so we have to square the amplitude here.
#            if ax is None:
#                ax = plt.subplot(nrows, 2, row*2-1)
#            utils.imshow_with_mouseover(ampl**2, ax=ax, extent=extent, cmap=cmap, norm=norm_amp)
#            if nrows == 1:
#                plt.title("Transmissivity for "+self.name)
#            plt.ylabel(units)
#            ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=4, integer=True))
#            ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=4, integer=True))
#            if colorbar: 
#                cb = plt.colorbar(ax.images[0], orientation=colorbar_orientation, ticks=[0,0.25, 0.5, 0.75, 1.0])
#                cb.set_label('Fraction')
#            if crosshairs:
#                ax.axhline(0,ls=":", color='k')
#                ax.axvline(0,ls=":", color='k')
#
#
#        if what=='phase' or what=='both':
#            if ax is None:
#                ax2 = plt.subplot(nrows, 2, row*2-1)
#            else:
#                ax2 = ax
#    
#            ax2 = plt.subplot(nrows, 2, row*2)
#            imshow_with_mouseover(opd, ax=ax2, extent=extent, cmap=cmap_opd, norm=norm_opd)
#            plt.ylabel(units)
#            ax2.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=4, integer=True))
#            ax2.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=4, integer=True))
#            if nrows == 1:
#                plt.title("OPD for "+self.name)
#            if colorbar:
#                cb = plt.colorbar(ax2.images[0], orientation=colorbar_orientation, ticks=np.array([-1, -0.5, 0, 0.5, 1])*opd_vmax)
#                cb.set_label('meters')
#            if crosshairs:
#                ax2.axhline(0,ls=":", color='k')
#                ax2.axvline(0,ls=":", color='k')

    def __str__(self):
        if self.planetype is _PUPIL:
            #return "Pupil plane: %s (%dx%d pixels, diam=%f m)" % (self.name, self.shape[0], self.shape[0], self.pupil_diam)
            return "Pupil plane: %s " % (self.name)
        elif self.planetype is _IMAGE:
            desc = "(%dx%d pixels, scale=%f arcsec/pixel)" % (self.shape[0], self.shape[0], self.pixelscale) if self.pixelscale is not None else "(Analytic)"
            return "Image plane: %s %s" % (self.name, desc)
        else:
            return "Optic: "+self.name

    @property
    def shape(self):
        """ Return shape of the OpticalElement, as a tuple """
        if hasattr(self, 'amplitude'):
            return self.amplitude.shape
        else: return None


class FITSOpticalElement(OpticalElement):
    """ Defines an arbitrary optic, based on amplitude transmission and/or OPD FITS files.

    This optic could be a pupil or field stop, an aberrated mirror, a phase mask, etc.
    The FITSOpticalElement class follows the behavior of the Wavefront class, using units
    of meters/pixel in pupil space and arcsec/pixel in image space.

    The interface is **very** flexible.  You can define a FITSOpticalElement either from 

    * a single FITS file giving the amplitude transmission (in which case phase is zero)
    * a single FITS file giving the OPD (in which case transmission is 1 everywhere)
    * two FITS files specifying both transmission and OPD.

    The FITS file argument(s) can be supplied either as 

        1. a string giving the path to a file on disk, 
        2. a FITS HDUlist object, or 
        3. in the case of OPDs, a tuple consisting of a path to a datacube and an integer index of a slice in that datacube. 

    A better interface for slice selection in datacubes is the transmission_index and opd_index keyword parameters listed below, 
    but the tuple interface is retained for back compatibility with existing code. 


    Parameters
    ----------
    name : string
        descriptive name for optic
    transmission, opd : string or fits HDUList
        Either FITS filenames *or* actual fits.HDUList objects for the transmission (from 0-1) and opd (in meters)
    transmission_slice, opd_slice : integers, optional
        If either transmission or OPD files are datacubes, you can specify the slice index using this argument.
    opdunits : string
        units for the OPD file. Default is 'meters'. can be 'meter', 'meters', 'micron(s)', 'nanometer(s)', or their SI abbreviations.
        If this keyword is not set explicitly, the BUNIT keyword in the FITS header will be checked. 
    verbose : bool
        whether to print stuff while computing
    planetype : int
        either _IMAGE or _PUPIL
    oversample : int
        how much to oversample beyond Nyquist.
    shift : tuple of floats, optional
        2-tuple containing X and Y fractional shifts for the pupil. These shifts are implemented by rounding them
        to the nearest integer pixel, and doing integer pixel shifts on the data array, without interpolation.
    rotation : float
        Rotation for that optic, in degrees counterclockwise. This is implemented using spline interpolation via
        the scipy.ndimage.interpolation.rotate function. 
    pixelscale : optical str or float
        By default, poppy will attempt to determine the appropriate pixel scale by examining the FITS header, 
        checking keywords "PUPLSCAL" and 'PIXSCALE' for pupil and image planes respectively. If you would like to
        override and use a different keyword, provide that as a string here. Alternatively, you can just set a 
        floating point value directly too (in meters/pixel or arcsec/pixel, respectively, for pupil or image planes).
    transmission_index, opd_index : ints, optional
        If the input transmission or OPD files are datacubes, provide a scalar index here for which cube 
        slice should be used. 



    *NOTE:* All mask files must be *squares*.

    Also, please note that the adopted convention is for the spectral throughput (transmission) to be given
    in appropriate units for acting on the *amplitude* of the electric field. Thus for example an optic with
    a uniform transmission of 0.5 will reduce the electric field amplitude to 0.5 relative to the input, 
    and thus reduce the total power to 0.25. This distinction only matters in the case of semitransparent
    (grayscale) masks. 



    """
 
    def __init__(self, name="unnamed optic", transmission=None, opd= None, opdunits="meters", 
            shift=None, rotation=None, pixelscale=None, planetype=None, 
            transmission_index=None, opd_index=None,
            **kwargs):

        OpticalElement.__init__(self,name=name, **kwargs)
        self.opd_file = None
        self.amplitude_file = None
        self.amplitude_header = None
        self.opd_header = None

        self.planetype=planetype


        _log.debug("Trans: "+str(transmission))
        _log.debug("OPD: "+str(opd))

        #---- Load amplitude transmission file. ---
        if opd is None and transmission is None:   # no input files, so just make a scalar
            _log.warn("No input files specified. You should set transmission=filename or opd=filename.")
            _log.warn("Creating a null optical element. Are you sure that's what you want to do?")
            self.amplitude = np.asarray([1.])
            self.opd = np.asarray([0.])
            self.pixelscale = None
            self.name = "-empty-"
        else:
            # load transmission file.
            if transmission is not None:
                if isinstance(transmission,str):
                    self.amplitude_file = transmission
                    self.amplitude, self.amplitude_header = fits.getdata(self.amplitude_file, header=True)
                    if self.name=='unnamed optic': self.name='Optic from '+self.amplitude_file
                    _log.info(self.name+": Loaded amplitude transmission from "+self.amplitude_file)
                elif isinstance(transmission,fits.HDUList):
                    self.amplitude_file='supplied as fits.HDUList object'
                    self.amplitude = transmission[0].data
                    self.amplitude_header = transmission[0].header
                    if self.name=='unnamed optic': self.name='Optic from fits.HDUList object'
                    _log.info(self.name+": Loaded amplitude transmission from supplied fits.HDUList object")
                else:
                    raise TypeError('Not sure how to use a transmission parameter of type '+str(type(transmission)))

                # check for datacube? 
                if len(self.amplitude.shape) > 2:
                    if transmission_index is None:
                        _log.info("The supplied pupil amplitude is a datacube but no slice was specified. Defaulting to use slice 0.")
                        transmission_index=0
                    self.amplitude_slice_index = transmission_index
                    self.amplitude = self.amplitude[self.amplitude_slice_index, :,:]
                    _log.debug(" Datacube detected, using slice ={0}".format(self.amplitude_slice_index))
            else:
                _log.debug("No transmission supplied - will assume uniform throughput = 1 ")
                # if transmission is none, wait until after OPD is loaded, below, and then create a matching
                # amplitude array uniformly filled with 1s. 


            #---- Load OPD file. ---
            if opd is None:
                # if only amplitude set, create an array of 0s with same size.
                self.opd = np.zeros(self.amplitude.shape)
                opdunits = 'meter' # doesn't matter, it's all zeros, but this will indicate no need to rescale below.

            elif isinstance(opd, fits.HDUList):
                # load from fits HDUList
                self.opd_file='supplied as fits.HDUList object'
                self.opd = opd[0].data
                self.opd_header = opd[0].header
                if self.name=='unnamed optic': self.name='OPD from supplied fits.HDUList object'
                _log.info(self.name+": Loaded OPD from supplied fits.HDUList object")
            elif isinstance(opd, basestring):
                # load from regular FITS filename
                self.opd_file=opd
                self.opd, self.opd_header = fits.getdata(self.opd_file, header=True)
                if self.name=='unnamed optic': self.name='OPD from '+self.opd_file
                _log.info(self.name+": Loaded OPD from "+self.opd_file)

            elif len(opd) ==2 and isinstance(opd[0], basestring) :
                # if OPD is specified as a 2-element iterable, treat the first element as the filename and 2nd as the slice of a cube.
                self.opd_file = opd[0]
                self.opd_slice = opd[1]
                self.opd, self.opd_header = fits.getdata(self.opd_file, header=True)
                self.opd = self.opd[self.opd_slice, :,:]
                if self.name=='unnamed optic': self.name='OPD from %s, plane %d' % (self.opd_file, self.opd_slice)
                _log.info(self.name+": Loaded OPD from  %s, plane %d" % (self.opd_file, self.opd_slice) )
            else:
                raise TypeError('Not sure how to use an OPD parameter of type '+str(type(transmission)))

            # check for datacube? 
            if len(self.opd.shape) > 2:
                if opd_index is None:
                    _log.info("The supplied pupil OPD is a datacube but no slice was specified. Defaulting to use slice 0.")
                    transmission_index=0
                self.opd_slice_index = transmission_index
                self.opd = self.opd[self.opd_slice_index, :,:]
                _log.debug(" Datacube detected, using slice ={0}".format(self.opd_slice_index))


            if transmission is None:
                _log.info("No info supplied on amplitude transmission; assuming uniform throughput = 1")
                self.amplitude = np.ones(self.opd.shape)

            # convert OPD into meters

            if opdunits is None:
                try:
                    opdunits = self.opd_header['BUNIT']
                except:
                    _log.error("No opdunit keyword supplied, and BUNIT keyword not found in header. Cannot determine OPD units")
                    raise StandardError("No opdunit keyword supplied, and BUNIT keyword not found in header. Cannot determine OPD units.")


            if opdunits.lower().endswith('s'): opdunits = opdunits[:-1] # drop trailing s if present
            if opdunits.lower() == 'meter' or opdunits.lower() == 'm':
                pass # no need to rescale
            elif opdunits.lower() == 'micron' or opdunits.lower() == 'um' or opdunits.lower() == 'micrometer':
                self.opd *= 1e-6
            elif opdunits.lower() == 'nanometer' or opdunits.lower() == 'nm':
                self.opd *= 1e-9



            if len (self.opd.shape) != 2 or self.opd.shape[0] != self.opd.shape[1]:
                _log.debug('OPD shape: '+str(self.opd.shape))
                raise ValueError, "OPD image must be 2-D and square"

            if len (self.amplitude.shape) != 2 or self.amplitude.shape[0] != self.amplitude.shape[1]:
                raise ValueError, "Pupil amplitude image must be 2-D and square"


            assert self.amplitude.shape == self.opd.shape
            assert self.amplitude.shape[0] == self.amplitude.shape[1]


            # if a shift is specified and we're NOT a null (scalar) optic, then do the shift:
            if shift is not None and len(self.amplitude.shape) ==2:
                if abs(shift[0]) > 0.5 or abs(shift[1])> 0.5:
                    raise ValueError("""You have asked for an implausibly large shift. Remember, shifts should be specified as
                      decimal values between -0.5 and 0.5, a fraction of the total optic diameter. """)
                rolly = int(np.round(self.amplitude.shape[0] * shift[1])) #remember Y,X order for shape, but X,Y order for shift
                rollx = int(np.round(self.amplitude.shape[1] * shift[0]))
                _log.info("Requested optic shift of (%6.3f, %6.3f) %%" % (shift))
                _log.info("Actual shift applied   = (%6.3f, %6.3f) %%" % (rollx*1.0/self.amplitude.shape[1], rolly *1.0/ self.amplitude.shape[0]))
                self._shift = (rollx*1.0/self.amplitude.shape[1], rolly *1.0/ self.amplitude.shape[0])

                self.amplitude = scipy.ndimage.shift(self.amplitude, (rolly, rollx)) 
                self.opd       = scipy.ndimage.shift(self.opd,       (rolly, rollx))
                #self.amplitude = scipy.ndimage.shift(self.amplitude, rollx, axis=1)
                #self.opd       = scipy.ndimage.shift(self.opd,       rollx, axis=1)

            # Likewise, if a rotation is specified and we're NOT a null (scalar) optic, then do the rotation:
            if rotation is not None and len(self.amplitude.shape) ==2:

                # do rotation with interpolation, but try to clean up some of the artifacts afterwards.
                # this is imperfect at best, of course...

                self.amplitude = scipy.ndimage.interpolation.rotate(self.amplitude, rotation, reshape=False).clip(min=0,max=1.0)
                wnoise = np.where(( self.amplitude < 1e-3) & (self.amplitude > 0))
                self.amplitude[wnoise] = 0
                self.opd       = scipy.ndimage.interpolation.rotate(self.opd,       rotation, reshape=False)
                _log.info("  Rotated optic by %f degrees counter clockwise." % rotation)
                #fits.PrimaryHDU(self.amplitude).writeto("test_rotated_amp.fits", clobber=True)
                #fits.PrimaryHDU(self.opd).writeto("test_rotated_opt.fits", clobber=True)
                self._rotation = rotation


            if pixelscale is None:
                pixelscale = 'PUPLSCAL' if self.planetype == _PUPIL else 'PIXSCALE' # set default FITS keyword
            if isinstance(pixelscale,str): # pixelscale is a str, so interpret it as a FITS keyword
                _log.debug("  Getting pixel scale from FITS keyword:" + pixelscale)
                try:
                    self.pixelscale = self.amplitude_header[pixelscale]
                except:
                    try:
                        self.pixelscale = self.opd_header[pixelscale]
                    except:
                        raise LookupError("Cannot find a FITS header keyword for pixelscale with the requested key="+pixelscale)
            else:  # pixelscale had better be a floating point value here.
                try:
                    _log.debug("  Getting pixel scale from user-provided float value:" + str(pixelscale))
                    self.pixelscale = float(pixelscale)
                except:
                    raise ValueError("pixelscale=%s is neither a FITS keyword string nor a floating point value." % str(pixelscale))


            if self.planetype == _PUPIL:
                self.pupil_diam = self.pixelscale * self.amplitude.shape[0] # needed to keep track of pupil array size, for now. Revisit in future versions?
            elif self.planetype == _IMAGE:
                pass

    @property
    def pupil_diam(self):
        return self.pixelscale * self.amplitude.shape[0]
    "Diameter of the pupil (if this is a pupil plane optic)"


class Rotation(OpticalElement):
    """ Performs a rotation of the axes in the optical train.

    This is not an actual optic itself, of course, but can be used to model
    a rotated optic by appling a Rotation before and/or after light is incident
    on that optic.


    This is basically a placeholder to indicate the need for a rotation at a
    given part of the optical train. The actual rotation computation is performed
    in the Wavefront object's propagation routines.


    Parameters
    ----------
    angle : float
        Rotation angle, counterclockwise. By default in degrees.
    units : 'degrees' or 'radians'
        Units for the rotation angle. 

    """
    def __init__(self, angle=0.0, units='degrees', **kwargs):
        if units == 'radians':
            angle*= np.pi/180
        elif units =='degrees':
            pass
        else:
            raise ValueError("Unknown value for units='%s'. Must be degrees or radians." % units)
        self.angle = angle

        OpticalElement.__init__(self, name= "Rotation by %.2f degrees" % angle, planetype=_ROTATION, **kwargs)


    def __str__(self):
        return "Rotation by %f degrees counter clockwise" % self.angle

    def getPhasor(self,wave):
        return 1.0  #no change in wavefront (apart from the rotation)
        # returning this is necessary to allow the multiplication in propagate_mono to be OK

    def display(self, nrows=1, row=1, **kwargs):
        plt.subplot(nrows, 2, row*2-1)
        plt.text(0.3,0.3,self.name)


class InverseTransmission(OpticalElement):
    """ Given any arbitrary OpticalElement with transmission T(x,y)
    return the inverse transmission 1 - T(x,y)

    This is a useful ingredient in the SemiAnalyticCoronagraph algorithm.
    """
    def __init__(self, optic=None):
        if optic is None or not hasattr(optic, 'getPhasor'):
            raise ValueError("Need to supply an valid optic to invert!")
        self.optic = optic
        self.name = "1 - "+optic.name
        self.planetype = optic.planetype
        self.shape = optic.shape
        self.pixelscale = optic.pixelscale
        self.oversample = optic.oversample
    def getPhasor(self, wave):
        return 1- self.optic.getPhasor(wave)


#------ Generic Analytic elements -----

class AnalyticOpticalElement(OpticalElement):
    """ Defines an abstract analytic optical element, i.e. one definable by some formula rather than
        by an input OPD or pupil file.

        This class is useless on its own; instead use its various subclasses that implement appropriate getPhasor functions.
        It exists mostly to provide some behaviors & initialization common to all analytic optical elements.

        Parameters
        ----------
        name, verbose, oversample, planetype : various
            Same as for OpticalElement
        transmission, opd : string
            These are *not allowed* for Analytic optical elements, and this class will raise an error if you try to set one.


    """
    def __init__(self,  **kwargs):
        OpticalElement.__init__(self, **kwargs)

        self.shape = None # no explicit shape required
        self.pixelscale = None

    def __str__(self):
        if self.planetype is _PUPIL:
            return "Pupil plane: %s (Analytic)" % (self.name)
        elif self.planetype is _IMAGE:
            return "Image plane: %s (Analytic)" % (self.name)
        else:
            return "Optic: "+self.name

    def getPhasor(self,wave):
        raise NotImplementedError("getPhasor must be supplied by a derived subclass")

    def sample(self, wavelength=2e-6, npix=512, grid_size=None, what='amplitude', return_scale=False, phase_unit='waves'):
        """ Sample the Analytic Optic onto a grid and return the array 
        
        Parameters
        ----------
        wavelength : float
            Wavelength in meters.
        npix : integer
            Number of pixels for sampling the array
        grid_size : float
            Field of view grid size (diameter) for sampling the optic, in meters for 
            pupil plane optics and arcseconds for image planes. Default value is
            taken from the optic's properties, if defined. Otherwise defaults to 
            6.5 meters or 2 arcseconds depending on plane.
        what : string
            What to return: optic 'amplitude' transmission, 'intensity' transmission, or 
            'phase'.  Note that phase with phase_unit = 'waves' should give the optical path difference, OPD.
        phase_unit : string
            Unit for returned phase array IF what=='phase'. One of 'radians', 'waves', 'meters'. 
        return_scale : float
            if True, will return a tuple containing the desired array and a float giving the pixel scale.
        """
        if self.planetype is _PUPIL:
            if grid_size is not None: diam = grid_size
            elif hasattr(self, 'pupil_diam'): diam = self.pupil_diam
            else : diam = 6.5  # meters
            w = Wavefront(wavelength=wavelength, npix=npix,  diam = diam)
            pixel_scale = diam/npix

        else:
            #unit="arcsec"

            if grid_size is not None: fov = grid_size
            elif hasattr(self, '_default_display_size'): fov = self._default_display_size
            else: fov=4
            pixel_scale = fov/npix
            w = Wavefront(wavelength=wavelength, npix=npix,  pixelscale = pixel_scale)

        phasor = self.getPhasor(w)
        _log.info("Computing {0} for {1} sampled onto {2} pixel grid".format(what, self.name, npix))
        if what =='amplitude':
            output_array = np.abs(phasor)
        elif what =='intensity':
            output_array = np.abs(phasor)**2
        elif what =='phase':
            if phase_unit == 'radians':
                output_array = np.angle(phasor) 
            elif phase_unit =='waves':
                output_array = np.angle(phasor) /(2*np.pi)
            elif phase_unit =='meters':
                output_array = np.angle(phasor) /(2*np.pi) * wavelength
            else:
                raise ValueError('Invalid/unknown phase_unit: '+phase_unit+". Must be one of [radians, waves, meters]")
        elif what =='complex':
            output_array = phasor
        else:
            raise ValueError('Invalid/unknown what to sample: '+what+". Must be one of [amplitude, intensity, phase, complex]")

        if return_scale:
            return (output_array, pixel_scale)
        else:
            return output_array


    def display(self, nrows=1, row=1,  wavelength=2e-6, npix=512,   **kwargs):
        """Display an Analytic optic by first computing it onto a grid...

        Parameters
        ----------
        wavelength : float
            Wavelength to evaluate this optic's properties at
        npix : int
            Number of pixels to use when sampling the analytic optical element.

        what : str
            What to display: 'intensity', 'phase', or 'both'
        ax : matplotlib.Axes instance 
            Axes to display into
        nrows, row : integers
            # of rows and row index for subplot display
        crosshairs : bool
            Display crosshairs indicating the center?
        colorbar : bool
            Show colorbar?
        colorbar_orientation : bool
            Desired orientation, horizontal or vertical?
            Default is horizontal if only 1 row of plots, else vertical
        opd_vmax : float
            Max value for OPD image display, in meters.
        title : string
            Plot label


        """
 

        _log.debug("Displaying "+self.name)
        phasor, pixelscale = self.sample(wavelength=wavelength, npix=npix, what='complex', return_scale=True) 

#        if self.planetype is _PUPIL:
#            #unit="meters"
#            if hasattr(self, 'pupil_diam'): self.diam = self.pupil_diam
#            else : self.diam = 6.5
#            halffov_x = self.diam/2
#            halffov_y = self.diam/2
#            w = Wavefront(wavelength=wavelength, npix=npix,  diam = self.diam)
#            self.pupil_scale = self.diam/npix
#            self.pixelscale = self.pupil_scale
#
#        else:
#            #unit="arcsec"
#            if hasattr(self, '_default_display_size'):
#                halffov_x = self._default_display_size/2
#                halffov_y = self._default_display_size/2
#            else:
#                halffov_x = 2.0
#                halffov_y = 2.0
#            self.pixelscale = 2.*halffov_x/npix
#            w = Wavefront(wavelength=wavelength, npix=npix,  pixelscale = self.pixelscale)
#

        # temporarily set attributes appropriately as if this were a regular OPticalElement
        #phasor = self.getPhasor(w)
        self.amplitude = np.abs(phasor)
        phase = np.angle(phasor) / (2*np.pi) 
        self.opd = phase *wavelength
        self.pixelscale = pixelscale

        #then call parent class display
        OpticalElement.display(self,nrows=nrows, row=row, **kwargs)

        # now un-set all the temporary attributes back, since this is analytic and these are unneeded
        self.pixelscale = None
        #self.pupil_scale = None
        #self.diam = None
        self.opd = None
        self.amplitude = None

    def toFITS(self, outname=None, what='amplitude', wavelength=2e-6, npix=512):
        """ Save an analytic optic computed onto a grid to a FITS file """

        if outname is None:
            raise ValueError("You must supply an output filename to write a FITS file.")

        output_array, pixelscale = self.sample(wavelength=wavelength, npix=npix, what=what, return_scale=True) 
        

#        if self.planetype is _PUPIL:
#            unit="meters"
#            if hasattr(self, 'pupil_diam'): self.diam = self.pupil_diam
#            else : self.diam = 6.5
#            halffov = self.diam/2
#            w = Wavefront(wavelength=wavelength, npix=npix,  diam = self.diam)
#            self.pupil_scale = self.diam/npix
#            self.pixelscale = self.pupil_scale
#
#        else:
#            unit="arcsec"
#            if hasattr(self, '_default_display_size'):
#                halffov = self._default_display_size/2
#            else:
#                halffov = 2.0
#            self.pixelscale = 2.*halffov/npix
#            w = Wavefront(wavelength=wavelength, npix=npix,  pixelscale = self.pixelscale)
#
#
#        # set attributes appropriately as if this were a regular OPticalElement
#        phasor = self.getPhasor(w)
#        self.amplitude = np.abs(phasor)
#        phase = np.angle(phasor) * 2*np.pi
#        self.opd = phase *wavelength

        phdu = fits.PrimaryHDU(output_array)
        phdu.header['OPTIC'] = self.name
        phdu.header['SOURCE'] = 'Computed with POPPY'
        phdu.header['CONTENTS'] = what
        phdu.header['PIXELSCL'] = pixelscale
        phdu.writeto(outname, clobber=True)
        _log.info("Output written to "+outname)


class ScalarTransmission(AnalyticOpticalElement):
    """ Either a null optic (empty plane) or some perfect ND filter...
    But most commonly this is just used as a null optic placeholder """
    def __init__(self, name="-empty-", transmission=1.0, **kwargs):
        AnalyticOpticalElement.__init__(self,name=name, **kwargs)
        self.transmission = transmission
    def getPhasor(self, wave):
        return self.transmission


#------ Analytic Image Plane elements -----

class BandLimitedCoron(AnalyticOpticalElement):
    """ Defines an ideal band limited coronagraph occulting mask.


        Parameters
        ----------
        name : string
            Descriptive name
        kind : string
            Either 'circular' or 'linear'. The linear ones are custom shaped to NIRCAM's design
            with flat bits on either side of the linear tapered bit.
            Also includes options 'nircamcircular' and 'nircamwedge' specialized for the
            JWST NIRCam occulters, including the off-axis ND acq spots and the changing
            width of the wedge occulter.
        sigma : float
            The numerical size parameter, as specified in Krist et al. 2009 SPIE
        wavelength : float
            Wavelength this BLC is optimized for, only for the linear ones.

    """
    def __init__(self, name="unnamed BLC", kind='circular', sigma=1, wavelength=None, **kwargs):
        AnalyticOpticalElement.__init__(self,name=name, planetype=_IMAGE, **kwargs)

        self.kind = kind.lower()        # either circular or linear
        if self.kind not in ['circular', 'linear', 'nircamwedge', 'nircamcircular']:
            raise ValueError("Invalid kind of BLC: "+self.kind)
        self.sigma = float(sigma)              # size parameter. See section 2.1 of Krist et al. SPIE 2007, 2009
        if wavelength is not None:
            self.wavelength = float(wavelength)    # wavelength, for selecting the linear wedge option only
        self._default_display_size= 20.        # default size for onscreen display, sized for NIRCam

    def getPhasor(self,wave):
        """ Compute the amplitude transmission appropriate for a BLC for some given pixel spacing
        corresponding to the supplied Wavefront.

        Based on the Krist et al. SPIE paper on NIRCam coronagraph design

        Note that the equations in Krist et al specify the intensity transmission of the occulter,
        but what we want to return here is the amplitude transmittance. That is the square root of the
        intensity, of course, so the equations as implemented here all differ from those written in
        Krist's SPIE paper by lacking an exponential factor of 2. Thanks to John Krist for pointing this out.

        """
        if not isinstance(wave, Wavefront):
            raise ValueError("BLC getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _IMAGE)

        y, x = wave.coordinates()
        if self.kind == 'circular':
            # larger sigma implies narrower peak? TBD verify if this is correct
            #
            r = np.sqrt(x**2+y**2)
            sigmar = self.sigma*r
            self.transmission = (1-  (2*scipy.special.jn(1,sigmar)/sigmar)**2)
        if self.kind == 'nircamcircular':
            # larger sigma implies narrower peak? TBD verify if this is correct
            #
            r = np.sqrt(x**2+y**2)
            sigmar = self.sigma*r
            sigmar.clip(np.finfo(sigmar.dtype).tiny)  # avoid divide by zero -> NaNs
            self.transmission = (1-  (2*scipy.special.jn(1,sigmar)/sigmar)**2)

            # add in the ND squares. Note the positions are not exactly the same in the two wedges.
            # See the figures  in Krist et al. of how the 6 ND squares are spaced among the 5 corongraph regions
            # Also add in the opaque border of the coronagraph mask holder.
            if self.sigma > 4:
                wnd = np.where((y > 5) & (((x < -5)&(x>-10)) | ((x > 7.5)&(x<12.5))) ) # MASK210R has one in the corner and one half in the other corner
                wborder  = np.where((np.abs(y) > 10) | (x < -10) )                        #left end of mask holder
            else:
                wnd = np.where((y > 5) & (np.abs(x) > 7.5) & (np.abs(x) < 12.5))        # the others have two halves on in each corner.
                wborder  = np.where(np.abs(y) > 10)

            self.transmission[wnd] = np.sqrt(1e-3)
            self.transmission[wborder] = 0


        elif self.kind == 'linear':
            #raise(NotImplemented("Generic linear not implemented"))
            sigmar = self.sigma * np.abs(y)
            sigmar.clip( np.finfo(sigmar.dtype).tiny)  # avoid divide by zero -> NaNs
            self.transmission = (1-  (np.sin(sigmar)/sigmar)**2)


        elif self.kind == 'nircamwedge':
            # This is hard-coded to the wedge-plus-flat-regions shape for NIRCAM

            # we want a scale factor that goes from  2 to 6 with 1/5th of it as a fixed part on either end
            #scalefact = np.linspace(1,7, x.shape[1]).clip(2,6)

            # the scale fact should depent on X coord in arcsec, scaling across a 20 arcsec FOV.
            # map flat regions to 2.5 arcsec each?
            # map -7.5 to 2, +7.5 to 6. slope is 4/15, offset is +9.5
            scalefact = (2+(-x+7.5)*4/15).clip(2,6)

            #scalefact *= self.sigma / 2 #;2.2513
            #scalefact *= 2.2513
            #scalefact.shape = (1, x.shape[1])
            # This does not work - shape appears to be curved not linear.
            # This is NOT a linear relationship. See calc_blc_wedge in test_poppy.

            if np.abs(self.wavelength - 2.1e-6) < 0.1e-6:
                polyfitcoeffs = np.array([  2.01210737e-04,  -7.18758337e-03,   1.12381516e-01,
                                          -1.00877701e+00,   5.72538509e+00,  -2.12943497e+01,
                                           5.18745152e+01,  -7.97815606e+01,   7.02728734e+01])
            elif np.abs(self.wavelength - 4.6e-6) < 0.1e-6:
                polyfitcoeffs = np.array([  9.16195583e-05,  -3.27354831e-03,   5.11960734e-02,
                                          -4.59674047e-01,   2.60963397e+00,  -9.70881273e+00,
                                           2.36585911e+01,  -3.63978587e+01,   3.20703511e+01])
            else:
                raise NotImplemented("No defined NIRCam wedge BLC mask for that wavelength?  ")

            sigmas = scipy.poly1d(polyfitcoeffs)(scalefact)

            sigmar = sigmas * np.abs(y)
            sigmar.clip( np.finfo(sigmar.dtype).tiny)  # avoid divide by zero -> NaNs
            self.transmission = (1-  (np.sin(sigmar)/sigmar)**2)
            # the bar should truncate at +- 10 arcsec:
            woutside = np.where(np.abs(x) > 10)
            self.transmission[woutside] = 1.0


            # add in the ND squares. Note the positions are not exactly the same in the two wedges.
            # See the figures  in Krist et al. of how the 6 ND squares are spaced among the 5 corongraph regions
            # Also add in the opaque border of the coronagraph mask holder.
            if np.abs(self.wavelength - 2.1e-6) < 0.1e-6:
                wnd = np.where((y > 5) & (((x < -5)&(x>-10)) | ((x > 7.5)&(x<12.5))) ) # half ND square on each side
                wborder  = np.where(np.abs(y) > 10)
            elif np.abs(self.wavelength - 4.6e-6) < 0.1e-6:
                wnd = np.where((y > 5) & (((x < -7.5)&(x>-12.5)) | (x > 5)) )
                wborder  = np.where((np.abs(y) > 10) | (x > 10) )     # right end of mask holder

            self.transmission[wnd] = np.sqrt(1e-3)
            self.transmission[wborder] = 0



        if not np.isfinite(self.transmission.sum()):
            #print "There are NaNs in the BLC mask - error!"
            #stop()
            _log.debug("There are NaNs in the BLC mask - correcting to zero. (DEBUG LATER?)")
            self.transmission[np.where(np.isfinite(self.transmission) == False)] = 0
        return self.transmission


class IdealFQPM(AnalyticOpticalElement):
    """ Defines an ideal 4-quadrant phase mask coronagraph, with its retardance
    set perfectly to 0.5 waves at one specific wavelength and varying linearly on
    either side of that.

    Parameters
    ----------
    name : string
        Descriptive name
    wavelength : float
        Wavelength in meters for which the FQPM was designed


    """
    def __init__(self, name="unnamed FQPM ", wavelength=10.65e-6, **kwargs):
        AnalyticOpticalElement.__init__(self, planetype=_IMAGE, **kwargs)
        self.name = name

        self.central_wavelength =wavelength

    def getPhasor(self,wave):
        """ Compute the amplitude transmission appropriate for a 4QPM for some given pixel spacing
        corresponding to the supplied Wavefront
        """

        if not isinstance(wave, Wavefront):
            raise ValueError("4QPM getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _IMAGE)

        # TODO this computation could be sped up a lot w/ optimzations
        phase = np.empty(wave.shape)
        n0 = wave.shape[0]/2
        phase[:n0,:n0]=0.5
        phase[n0:,n0:]=0.5
        phase[n0:,:n0]=0
        phase[:n0,n0:]=0

        retardance = phase*self.central_wavelength/wave.wavelength

        #outFITS = fits.HDUList(fits.PrimaryHDU(retardance))
        #outFITS.writeto('retardance_fqpm.fits', clobber=True)
        #_log.info("Retardance is %f waves" % retardance.max())
        FQPM_phasor = np.exp(1.j * 2* np.pi * retardance)
        return FQPM_phasor


class IdealRectangularFieldStop(AnalyticOpticalElement):
    """ Defines an ideal rectangular field stop

    Parameters
    ----------
    name : string
        Descriptive name
    width, height: float
        Size of the field stop, in arcseconds. Default 0.5 width, height 5.
    angle : float
        Position angle of the field stop sides relative to the detector +Y direction, in degrees.

    """

    def __init__(self, name="unnamed field stop",  width=0.5, height=5.0, angle=0, **kwargs):
        AnalyticOpticalElement.__init__(self, planetype=_IMAGE, **kwargs)
        self.name = name
        self.width = float(width)            # width of square stop in arcseconds.
        self.height = float(height)            # height of square stop in arcseconds.
        self.angle = float(angle)
        self._default_display_size = max(height, width)*1.2

    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the field stop.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("IdealFieldStop getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _IMAGE)

        y, x= wave.coordinates()
        xnew =  x*np.cos(np.deg2rad(self.angle)) + y*np.sin(np.deg2rad(self.angle))
        ynew = -x*np.sin(np.deg2rad(self.angle)) + y*np.cos(np.deg2rad(self.angle))
        x,y = xnew, ynew


        w_outside = np.where( (abs(y) > (self.height/2))  | (abs(x) > (self.width/2)))
        del x # for large arrays, cleanup very promptly, before allocating self.transmission
        del y
        self.transmission = np.ones(wave.shape)
        self.transmission[w_outside] = 0

        return self.transmission


class IdealFieldStop(AnalyticOpticalElement):
    """ Defines an ideal square field stop

    Parameters
    ----------
    name : string
        Descriptive name
    size : float
        Size of the field stop, in arcseconds. Default 20.
    angle : float
        Position angle of the field stop sides relative to the detector +Y direction, in degrees.

    """

    def __init__(self, name="unnamed field stop",  size=20., angle=0, **kwargs):
        AnalyticOpticalElement.__init__(self, planetype=_IMAGE, **kwargs)
        self.name = name
        self.size = size            # size of square stop in arcseconds.
        #self.pixelscale=0
        self.angle=angle
        self._default_display_size = size*1.2

    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the field stop.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("IdealFieldStop getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _IMAGE)

        #phasor = np.zeros(wave.shape)
        y, x= wave.coordinates()
        #y, x = np.indices(wave.shape)
        #y -= wave.shape[0]/2
        #x -= wave.shape[1]/2
        xnew =  x*np.cos(np.deg2rad(self.angle)) + y*np.sin(np.deg2rad(self.angle))
        ynew = -x*np.sin(np.deg2rad(self.angle)) + y*np.cos(np.deg2rad(self.angle))
        x,y = xnew, ynew
        #del xnew
        #del ynew


        halfsize = self.size   / 2
        w_outside = np.where( (abs(y) > halfsize)  | (abs(x) > halfsize))
        del x # for large arrays, cleanup very promptly, before allocating self.transmission
        del y
        self.transmission = np.ones(wave.shape)
        self.transmission[w_outside] = 0

        return self.transmission


class IdealCircularOcculter(AnalyticOpticalElement):
    """ Defines an ideal circular occulter (opaque circle)

    Parameters
    ----------
    name : string
        Descriptive name
    radius : float
        Radius of the occulting spot, in arcseconds. Default is 1.0

    """

    def __init__(self, name="unnamed occulter",  radius=1.0, **kwargs):
        AnalyticOpticalElement.__init__(self, planetype=_IMAGE, **kwargs)
        self.name = name
        self.radius = radius    # radius of circular occulter in arcseconds.
        self._default_display_size = 10
        #self.pixelscale=0

    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the occulter.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _IMAGE)

        y, x= wave.coordinates()
        #y, x = np.indices(wave.shape)
        #y -= wave.shape[0]/2
        #x -= wave.shape[1]/2
        r = np.sqrt(x**2+y**2) #* wave.pixelscale
        w_inside = np.where( r <= self.radius)

        del x
        del y
        del r
        self.transmission = np.ones(wave.shape)
        self.transmission[w_inside] = 0

        return self.transmission


class IdealBarOcculter(AnalyticOpticalElement):
    """ Defines an ideal bar occulter (like in MIRI's Lyot coronagraph)

    Parameters
    ----------
    name : string
        Descriptive name
    width : float
        width of the bar stop, in arcseconds. Default is 1.0
    angle : float
        position angle of the bar, rotated relative to the normal +y direction.

    """

    def __init__(self, name="bar occulter",  width=1.0, angle= 0, **kwargs):
        AnalyticOpticalElement.__init__(self, planetype=_IMAGE, **kwargs)
        self.name = name
        self.width = width
        self.angle = angle
        #self.pixelscale=0
        self._default_display_size = 10

    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the occulter.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _IMAGE)

        y, x= wave.coordinates()
        #y, x = np.indices(wave.shape)
        #y -= wave.shape[0]/2
        #x -= wave.shape[1]/2

        xnew = x*np.cos(np.deg2rad(self.angle)) + y*np.sin(np.deg2rad(self.angle))
        del x
        del y
        w_inside = np.where( np.abs(xnew) <= self.width/2)
        del xnew
        self.transmission = np.ones(wave.shape)
        self.transmission[w_inside] = 0

        return self.transmission


#------ Analytic Pupil Plane elements -----

class FQPM_FFT_aligner(AnalyticOpticalElement):
    """  Helper class for modeling FQPMs accurately

    Adds (or removes) a slight wavelength- and pixel-scale-dependent tilt
    to a pupil wavefront, to ensure the correct alignment of the image plane
    FFT'ed PSF with the desired quad pixel alignment for the FQPM.

    This is purely a computational convenience tool to work around the
    pixel coordinate restrictions imposed by the FFT algorithm,
    not a representation of any physical optic.

    Parameters
    ----------
    direction : string
        'forward' or 'backward'

    """
    def __init__(self, name="FQPM FFT aligner", direction='forward', **kwargs):
        AnalyticOpticalElement.__init__(self, name=name, planetype=_PUPIL, **kwargs)
        direction = direction.lower()
        if direction != 'forward' and direction !='backward': raise ValueError("Invalid direction %s, must be either forward or backward." % direction)
        self.direction = direction
        self._suppress_display=True
        #self.displayable = False

    def getPhasor(self,wave):
        """ Compute the required tilt needed to get the PSF centered on the corner between
        the 4 central pixels, not on the central pixel itself.
        """

        if not isinstance(wave, Wavefront):
            raise ValueError("FQPM getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL )


        fft_im_pixelscale = wave.wavelength/ wave.diam / wave.oversample * _RADIANStoARCSEC
        required_offset = -fft_im_pixelscale *0.5
        if self.direction == 'backward':
            required_offset *= -1
            wave._image_centered='pixel'
        else:
            wave._image_centered='corner'
        wave.tilt(required_offset, required_offset)

        # gotta return something... so return a value that will not affect the wave any more.
        align_phasor = 1.0
        return align_phasor


class ParityTestAperture(AnalyticOpticalElement):
    """ Defines a circular pupil aperture with boxes cut out.
    This is mostly a test aperture, which has no symmetry and thus can be used to
    test the various Fourier transform algorithms and sign conventions.

    Parameters
    ----------
    name : string
        Descriptive name
    radius : float
        Radius of the pupil, in meters. Default is 1.0

    pad_factor : float, optional
        Amount to oversize the wavefront array relative to this pupil.
        This is in practice not very useful, but it provides a straightforward way
        of verifying during code testing that the amount of padding (or size of the circle)
        does not make any numerical difference in the final result.

    """

    def __init__(self, name=None,  radius=1.0, pad_factor = 1.5, **kwargs):
        if name is None: name = "Circle, radius=%.2f m" % radius
        AnalyticOpticalElement.__init__(self,name=name, planetype=_PUPIL, **kwargs)
        self.radius = radius
        self.pupil_diam = pad_factor * 2* self.radius # for creating input wavefronts - let's pad a bit


    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the occulter.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("CircularAperture getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL)

        y, x = wave.coordinates()
        r = np.sqrt(x**2+y**2) #* wave.pixelscale
        del x
        del y


        w_outside = np.where( r > self.radius)
        del r
        self.transmission = np.ones(wave.shape)
        self.transmission[w_outside] = 0

        w_box1 = np.where( r> (self.radius*0.5) & np.abs(x) < self.radius*0.1 & y < 0)
        w_box1 = np.where( r> (self.radius*0.75) & np.abs(y) < self.radius*0.2 & x< 0)
        self.transmission[w_box1] = 0
        self.transmission[w_box2] = 0

        return self.transmission


class CircularAperture(AnalyticOpticalElement):
    """ Defines an ideal circular pupil aperture

    Parameters
    ----------
    name : string
        Descriptive name
    radius : float
        Radius of the pupil, in meters. Default is 1.0

    pad_factor : float, optional
        Amount to oversize the wavefront array relative to this pupil.
        This is in practice not very useful, but it provides a straightforward way
        of verifying during code testing that the amount of padding (or size of the circle)
        does not make any numerical difference in the final result.

    """

    def __init__(self, name=None,  radius=1.0, pad_factor = 1.5, **kwargs):
        if name is None: name = "Circle, radius=%.2f m" % radius
        AnalyticOpticalElement.__init__(self,name=name, planetype=_PUPIL, **kwargs)
        self.radius = radius
        self.pupil_diam = pad_factor * 2* self.radius # for creating input wavefronts - let's pad a bit


    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the occulter.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("CircularAperture getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL)

        y, x = wave.coordinates()
        #y, x = np.indices(wave.shape)
        #y -= wave.shape[0]/2
        #x -= wave.shape[1]/2
        r = np.sqrt(x**2+y**2) #* wave.pixelscale
        del x
        del y


        w_outside = np.where( r > self.radius)
        del r
        self.transmission = np.ones(wave.shape)
        self.transmission[w_outside] = 0

        #_log.debug("Circle: radius = %.3f m = %.3f pixels " % (self.radius, self.radius/wave.pixelscale))
        #_log.debug("        center = %f" % (wave.shape[0]/2 ))
        #w_inside = np.where( r <= self.radius)
        #_log.debug("        minx: %d" % w_inside[0].min())
        #stop()

        return self.transmission


class HexagonAperture(AnalyticOpticalElement):
    """ Defines an ideal hexagonal pupil aperture

    Specify either the side length (= corner radius) or the
    flat-to-flat distance.

    Parameters
    ----------
    name : string
        Descriptive name
    flattoflat : float, optional
        Flat-to-flat distance of the pupil, in meters. Default is 1.0
    side : float, optional
        side length of hexagon, in meters. Overrides flattoflat if both are present.

    """

    def __init__(self, name=None,  flattoflat=None, side=None, **kwargs):
        if flattoflat is None and side is None:
            self.side = 1.0
        elif side is not None:
            self.side = float(side)
        else:
            self.side = float(flattoflat)/np.sqrt(3.)
        self.pupil_diam = 2* self.side # for creating input wavefronts
        if name is None: name = "Hexagon, side length= %.1f m" % self.side

        AnalyticOpticalElement.__init__(self,name=name, planetype=_PUPIL, **kwargs)


    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the occulter.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("CircularAperture getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL)

        y, x = wave.coordinates()
        absy = np.abs(y)

        self.transmission = np.zeros(wave.shape)

        w_rect = np.where((np.abs(x) <= 0.5*self.side) & (absy <= np.sqrt(3)/2*self.side))
        w_left_tri  = np.where(   (x<=-0.5*self.side) & (x >= -1*self.side) & (absy <= (x+1*self.side)*np.sqrt(3)))
        w_right_tri = np.where(   (x>= 0.5*self.side) & (x <=  1*self.side) & (absy <= (1*self.side-x)*np.sqrt(3)))
        self.transmission[w_rect] = 1
        self.transmission[w_left_tri] = 1
        self.transmission[w_right_tri] = 1

        return self.transmission


class MultiHexagonAperture(AnalyticOpticalElement):
    """ Defines a hexagonally segmented aperture

    Parameters
    ----------
    name : string
        Descriptive name
    rings : integer
        The number of rings of hexagons to include (
        i.e. 2 for a JWST-like aperture, 3 for a Keck-like aperture, and so on)
    flattoflat : float, optional
        Flat-to-flat distance a single hexagonal segment, in meters. Default is 1.0
    gap: float, optional
        Gap between adjacent segments, in meters. Default is 0.01 m = 1 cm
    center : bool, optional
        should the central segment be included? Default is False. 
    segmentlist : list of ints, optional
        This allows one to specify that only a subset of segments are present, for a
        partially populated segmented telescope, non-redundant segment set, etc. 
        Segments are numbered from 0 for the center segment, 1 for the segment immediately
        above it, and then clockwise around each ring. 
        For example, segmentlist=[1,3,5] would make an aperture of 3 segments. 


    Note that this routine becomes a bit slow for nrings >4. For repeated computations on
    the same aperture, it will be faster to create this once, save it to a FITS file using
    the toFITS() method, and then use that. 

    """


    def __init__(self, name="MultiHex",  flattoflat=1.0, gap=0.01, rings=1, segmentlist=None, center=False, **kwargs):
        self.flattoflat=float(flattoflat)
        self.side = float(flattoflat)/np.sqrt(3.)
        self.rings=rings
        self.gap=gap
        #self._label_values = True # undocumented feature to draw hex indexes into the array
        AnalyticOpticalElement.__init__(self,name=name, planetype=_PUPIL, **kwargs)

        self.pupil_diam = (self.flattoflat+self.gap) * (2*self.rings+1)

        # make a list of all the segments included in this hex aperture
        if segmentlist is not None:
            self.segmentlist=segmentlist
        else:
            self.segmentlist = range(self._nHexesInsideRing(self.rings+1))
            if not center: self.segmentlist.remove(0) # remove center segment 0
          

    def _nHexesInRing(self, n):
        """ How many hexagons in ring N? """
        return 1 if n==0 else 6*n

    def _nHexesInsideRing(self,n):
        """ How many hexagons interior to ring N, not counting N?"""
        return sum( [self._nHexesInRing(i) for i in range(n)])

    def _hexInRing(self, hex_index):
        """ What ring is a given hexagon in?"""
        interior_rings=0
        if hex_index ==0: return 0
        for i in range(100):
            #print i
            if self._nHexesInsideRing(i) <= hex_index and self._nHexesInsideRing(i+1) > hex_index:
                return i
        return "Loop exceeded!"

    def _hexRadius(self, hex_index):
        " Radius of a given hexagon from the center "
        ring = self._hexInRing(hex_index)
        if ring <=1: return (self.flattoflat+self.gap) * ring

    def _hexCenter(self, hex_index):
        """ Center coordinates of a given hexagon 
        counting clockwise around each ring

        Returns y, x coords

        """
        ring = self._hexInRing(hex_index)

        # now count around from the starting point:
        index_in_ring = hex_index - self._nHexesInsideRing(ring) +1  # 1-based
        #print "hex %d is %dth in its ring" % (hex_index, index_in_ring)

        angle_per_hex = 2*np.pi/self._nHexesInRing(ring) # angle in radians

        # Now figure out what the radius is:
        xpos = None
        if ring <=1: 
            radius =  (self.flattoflat+self.gap) * ring
            angle = angle_per_hex * (index_in_ring-1)
        elif ring ==2:
            if np.mod(index_in_ring,2) == 1: radius = (self.flattoflat+self.gap) * ring # JWST 'B' segments
            else: radius = radius = self.side*3 + self.gap*np.sqrt(3.)/2*2              # JWST 'C' segments
            angle = angle_per_hex * (index_in_ring-1)
        elif ring==3:
            if np.mod(index_in_ring,ring) == 1: 
                radius = (self.flattoflat+self.gap) * ring # JWST 'B' segments
                angle = angle_per_hex * (index_in_ring-1)
            else: # C-like segments (in pairs)
                ypos = 2.5*(self.flattoflat+self.gap)
                xpos = 1.5*self.side + self.gap* np.sqrt(3)/4  
                radius = np.sqrt(xpos**2+ ypos**2) # (self.flattoflat+self.gap) * ring # JWST 'B' segments
                Cangle = np.arctan2(xpos, ypos)

                if np.mod(index_in_ring,3) == 2:
                    last_B_angle = ((index_in_ring-1)//3)*3 * angle_per_hex
                    angle = last_B_angle + Cangle* np.mod(index_in_ring-1,3)
                else:
                    next_B_angle = (((index_in_ring-1)//3)*3+3) * angle_per_hex
                    angle = next_B_angle - Cangle #* np.mod(index_in_ring-1,3)
                xpos=None
        else: # generalized code!
            # the above are actuall now redundant given that this exists, but
            # I'll leave them alone for now. 

            whichside = (index_in_ring-1)//ring # which of the sides are we on?

            if np.mod(index_in_ring,ring) == 1: 
                radius = (self.flattoflat+self.gap) * ring # JWST 'B' segments
                angle = angle_per_hex * (index_in_ring-1)
            else: 
                # find position of previous 'B' type segment.
                radius0 = (self.flattoflat+self.gap) * ring # JWST 'B' segments
                last_B_angle = ((index_in_ring-1)//ring)*ring * angle_per_hex
                #angle0 = angle_per_hex * (index_in_ring-1)
                ypos0 = radius0 * np.cos(last_B_angle)
                xpos0 = radius0 * np.sin(last_B_angle)

                da = (self.flattoflat+self.gap)*np.cos(30*np.pi/180)
                db = (self.flattoflat+self.gap)*np.sin(30*np.pi/180)

        
                if whichside ==0: 
                    dx, dy = da, -db
                elif whichside==1:
                    dx, dy = 0, -(self.flattoflat+self.gap)
                elif whichside==2:
                    dx, dy = -da, -db
                elif whichside==3:
                    dx, dy = -da, db
                elif whichside==4:
                    dx, dy = 0, (self.flattoflat+self.gap)
                elif whichside==5:
                    dx, dy = da, db
 
                xpos = xpos0+dx*np.mod(index_in_ring-1, ring)
                ypos = ypos0+dy*np.mod(index_in_ring-1, ring)
                #print index_in_ring, whichside, np.mod(index_in_ring-1, ring),  xpos, ypos

        # now clock clockwise around the ring (for rings <=3 only)
        if xpos is None:
            ypos = radius * np.cos(angle)
            xpos = radius * np.sin(angle)

        return ypos, xpos



    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the occulter.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL)

        y, x = wave.coordinates()
        absy = np.abs(y)

        self.transmission = np.zeros(wave.shape)

        for i in self.segmentlist:
            self._oneHexagon(wave, i)


        return self.transmission

    def _oneHexagon(self, wave, index):
        """ Draw one hexagon into the self.transmission array """

        y, x = wave.coordinates()

        ceny, cenx = self._hexCenter(index)

        y-= ceny
        x -=cenx
        absy = np.abs(y)

        w_rect = np.where((np.abs(x) <= 0.5*self.side) & (absy <= np.sqrt(3)/2*self.side))
        w_left_tri  = np.where(   (x<=-0.5*self.side) & (x >= -1*self.side) & (absy <= (x+1*self.side)*np.sqrt(3)))
        w_right_tri = np.where(   (x>= 0.5*self.side) & (x <=  1*self.side) & (absy <= (1*self.side-x)*np.sqrt(3)))

        #val = np.sqrt(float(index)) if self._label_values else 1
        val=1
        #if self._label_values: print "value: ", val
        self.transmission[w_rect] = val
        self.transmission[w_left_tri] = val
        self.transmission[w_right_tri] = val


class NgonAperture(AnalyticOpticalElement):
    """ Defines an ideal N-gon pupil aperture. 

    Parameters
    -----------
    name : string
        Descriptive name
    nsides : integer
        Number of sides. Default is 6.
    radius : float
        radius to the vertices, meters. Default is 1. 
    rotation : float
        Rotation angle to first vertex. Default is 0.
    """
    def __init__(self, name=None,  nsides=6, radius=1, rotation=0., **kwargs):
        self.radius =radius
        self.nsides =nsides
        self.rotation =rotation
        self.pupil_diam = 2* self.radius # for creating input wavefronts
        if name is None: name = "%d-gon, radius= %.1f m" % (self.nsides, self.radius)
        AnalyticOpticalElement.__init__(self,name=name, planetype=_PUPIL, **kwargs)

    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the occulter.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL)
        y, x = wave.coordinates()

        phase = self.rotation *np.pi/180
        vertices = np.array( [ [np.cos( i * 2*np.pi/self.nsides +phase ), np.sin( i * 2*np.pi/self.nsides +phase)] for i in range(self.nsides)], float)

        self.transmission = np.zeros(wave.shape)
        for row in range(wave.shape[0]):
            pts = np.asarray(zip(x[row],y[row]))
            ok = matplotlib.nxutils.points_inside_poly(pts, vertices)
            self.transmission[row][ok] = 1.0


        return self.transmission


class RectangleAperture(AnalyticOpticalElement):
    """ Defines an ideal rectangular pupil aperture

    Parameters
    ----------
    name : string
        Descriptive name
    width : float
        width of the rectangle, in meters. Default is 0.5
    height : float
        height of the rectangle, in meters. Default is 1.0
    rotation : float
        Rotation angle for 'width' axis. Default is 0.

    """

    def __init__(self, name=None,  width=0.5, height=1.0, rotation=0.0, **kwargs):
        self.width=width
        self.height=height
        self.rotation =rotation
        if name is None: name = "Rectangle, size= {s.width:.1f} m wide * {s.height:.1f} m high".format(s=self)
        AnalyticOpticalElement.__init__(self,name=name, planetype=_PUPIL, **kwargs)
        self.pupil_diam = np.sqrt(self.height**2 + self.width**2) # for creating input wavefronts

    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the occulter.
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL)

        y, x = wave.coordinates()

        if self.rotation != 0 :
            angle = np.deg2rad(self.rotation)
            xp =  np.cos(angle) * x + np.sin(angle) * y
            yp = -np.sin(angle) * x + np.cos(angle) * y

            x = xp
            y = yp


        w_outside = np.where( (abs(y) > (self.height/2)) | (abs(x) > (self.width/2)) )
        del y
        del x

        self.transmission = np.ones(wave.shape)
        self.transmission[w_outside] = 0
        return self.transmission


class SquareAperture(RectangleAperture):
    """ Defines an ideal square pupil aperture

    Parameters
    ----------
    name : string
        Descriptive name
    size: float
        side length of the square, in meters. Default is 1.0
    rotation : float
        Rotation angle for the square. Default is 0.


    """

    def __init__(self, name=None,  size=1.0, **kwargs):
        self._size=size
        if name is None: name = "Square, side length= %.1f m" % size*2
        RectangleAperture.__init__(self,width=size, height=size,**kwargs)
        self.size = size
        self.pupil_diam = 2* self.size # for creating input wavefronts

    @property
    def size(self):
        return self._size
    @size.setter
    def size(self, value):
        self._size=value
        self.height=value
        self.width=value


class SecondaryObscuration(AnalyticOpticalElement):
    """ Defines the central obscuration of an on-axis telescope including secondary mirror and supports

    The number of supports is adjustible but they are always radially symmetric around the center.

    Parameters
    ----------
    secondary_radius : float
        Radius of the circular secondary obscuration. Default 0.5 m
    n_supports : int
        Number of secondary mirror supports ("spiders"). These will be 
        spaced equally around a circle.  Default is 4.
    support_width : float
        Width of each support, in meters. Default is 0.01 m = 1 cm.
    """

    def __init__(self, name=None,  secondary_radius=0.5, n_supports=4, support_width=0.01, **kwargs):
        if name is None: name = "Secondary Obscuration with {0} supports".format(n_supports)
        AnalyticOpticalElement.__init__(self,name=name, planetype=_PUPIL, **kwargs)
        self.secondary_radius=secondary_radius
        self.n_supports = n_supports
        self.support_width = support_width

        self.pupil_diam = 4* self.secondary_radius # for creating input wavefronts if this is the first optic in a opticalsystem

    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the obscuration
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL)

        self.transmission = np.ones(wave.shape)

        y, x = wave.coordinates()
        r = np.sqrt(x**2+y**2) #* wave.pixelscale

        self.transmission[r < self.secondary_radius] = 0

        for i in range(self.n_supports):
            angle = 2*np.pi / self.n_supports * i

            # calculate rotated x' and y' coordinates after rotation by that angle.
            xp =  np.cos(angle) * x + np.sin(angle) * y
            yp = -np.sin(angle) * x + np.cos(angle) * y

            self.transmission[ (xp > 0) & (np.abs(yp) < self.support_width/2)] = 0

            # TODO check here for if there are no pixels marked because the spider is too thin. In that case use a grey scale approximation

        return self.transmission


class AsymmetricSecondaryObscuration(SecondaryObscuration):
    """ Defines a central obscuration with one or more supports which can be oriented at
    arbitrary angles around the primary mirror, a la the three supports of JWST

    Parameters
    ----------
    secondary_radius : float
        Radius of the circular secondary obscuration. Default 0.5 m
    support_angle : ndarray or list of floats
        The angle measured counterclockwise from +Y for each support
    support_width : float, or list of floats
        if scalar, gives the width for all support struts
        if a list, gives separately the width for each support strut independently.
        Widths in meters. Default is 0.01 m = 1 cm.
    """
    def __init__(self, support_angle=[0, 90, 240], support_width=0.01, **kwargs):
        SecondaryObscuration.__init__(self, n_supports = len(support_angle), **kwargs)

        self.support_angle = np.asarray(support_angle)
        if np.isscalar(support_width): support_width = np.zeros(len(support_angle)) + support_width
        self.support_width  = support_width

    def getPhasor(self,wave):
        """ Compute the transmission inside/outside of the obscuration
        """
        if not isinstance(wave, Wavefront):
            raise ValueError("getPhasor must be called with a Wavefront to define the spacing")
        assert (wave.planetype == _PUPIL)

        self.transmission = np.ones(wave.shape)

        y, x = wave.coordinates()
        r = np.sqrt(x**2+y**2) #* wave.pixelscale

        self.transmission[r < self.secondary_radius] = 0

        for angle_deg, width in zip(self.support_angle, self.support_width):
            angle = np.deg2rad(angle_deg + 90)  # 90 deg offset is to start from the +Y direction

            # calculate rotated x' and y' coordinates after rotation by that angle.
            xp =  np.cos(angle) * x + np.sin(angle) * y
            yp = -np.sin(angle) * x + np.cos(angle) * y

            self.transmission[ (xp > 0) & (np.abs(yp) < width/2)] = 0

            # TODO check here for if there are no pixels marked because the spider is too thin. In that case use a grey scale approximation

        return self.transmission




class ThinLens(AnalyticOpticalElement):
    """ An idealized thin lens, implemented as a Zernike defocus term.

    Parameters
    -------------
    nwaves : float
        The number of waves of defocus, peak to valley. May be positive or negative.
        This is applied as a normalization over an area defined by the circumscribing circle
        of the input wavefront. That is, there will be nwaves defocus peak-to-valley
        over the region of the pupil that has nonzero input intensity.
    reference_wavelength : float
        Wavelength, in meters, at which that number of waves of defocus is specified. 

    """
    def __init__(self, name='Thin lens', nwaves=4.0, reference_wavelength=2e-6, **kwargs):
        AnalyticOpticalElement.__init__(self,name=name, planetype=_PUPIL, **kwargs)

        self.reference_wavelength = reference_wavelength
        self.nwaves = nwaves
        self.max_phase_delay = reference_wavelength * nwaves


    def getPhasor(self, wave):
        y, x = wave.coordinates()
        r = np.sqrt(x**2+y**2)
        # get the normalized radius, assuming the input wave
        # is a square
        #if wave.shape[0] != wave.shape[1]:
            #raise NotImplemented("ThinLens only implemented for square wavefronts.")
        max_r = r[np.where(wave.intensity > 0)].max()
        r_norm = r / max_r 
        #r_norm = r / (wave.shape[0]/2.*wave.pixelscale)

        #defocus_zernike = np.sqrt(3)* (2* r_norm**2 - 1)  *  (self.nwaves *self.reference_wavelength/wave.wavelength)
        # don't forget the factor of 0.5 to make the scaling factor apply as peak-to-valley rather than center-to-peak
        defocus_zernike = (2* r_norm**2 - 1)  *  (0.5 * self.nwaves *self.reference_wavelength/wave.wavelength)
        lens_phasor = np.exp(1.j * 2* np.pi * defocus_zernike)
        #stop()
        return lens_phasor


#------ generic analytic optics ------

class CompoundAnalyticOptic(AnalyticOpticalElement):
    """ Define a compound analytic optical element made up of the combination
    of two or more individual optical elements.

    This is just a convenience routine for semantic organization of optics.
    It can be useful to keep the list of optical planes cleaner, but
    you can certainly just add a whole bunch of planes all in a row without
    using this class to group them.

    All optics should be of the same plane type (pupil or image); propagation between
    different optics contained inside one compound is not supported.

    Parameters
    ----------
    opticslist : list
        A list of AnalyticOpticalElements to be merged together.

    """
    def __init__(self, opticslist=None, name="unnamed", verbose=True, **kwargs):
        if opticslist is None:
            raise ValueError("Missing required opticslist argument to CompoundAnalyticOptic")
        AnalyticOpticalElement.__init__(self,name=name, verbose=verbose, **kwargs)

        #self.operation = operation
        self.opticslist = []
        self._default_display_size = 3
        self.planetype= None
        for optic in opticslist:

            if not (isinstance(optic, AnalyticOpticalElement) 
                or (isinstance(optic,InverseTransmission) and isinstance(optic.optic,AnalyticOpticalElement))):  #an inverted analytic element is also OK here
                raise ValueError("Supplied optics list to CompoundAnalyticOptic can only contain AnalyticOptics")
            else:
                # if we are adding the first optic in the list, check what type of optical plane it has
                # for subsequent optics, validate they have the same type
                if len(self.opticslist)  == 0:
                    self.planetype = optic.planetype 
                elif self.planetype != optic.planetype: 
                    raise ValueError("Cannot mix image plane and pupil plane optics in the same CompoundAnalyticOptic")


                self.opticslist.append(optic)
                if hasattr(optic, '_default_display_size'):
                    self._default_display_size = max(self._default_display_size, optic._default_display_size)

        if self.planetype == _PUPIL:
            self.pupil_diam = np.asarray( [o.pupil_diam for o in self.opticslist]).max()

    def getPhasor(self,wave):
        #phasor = self.opticslist[0].getPhasor(wave)

        ampl = np.ones(wave.shape)
        opd = np.zeros(wave.shape)
        for optic in self.opticslist:
            #nextphasor = optic.getPhasor(wave)
            #phasor *= nextphasor #FIXME this does not work... for instance if you have an aperture mask (so all the phase is zero)
                                  # then you really want to multiply the amplitude transmissions and add the phases.
                                  # simpler to just multiply the wave instead here:
            wave *= optic

            #revised version: handle amplitude and OPD both explictly here
            nextphasor = optic.getPhasor(wave)
            nextamp = np.abs(nextphasor)
            nextopd = np.angle(nextphasor) * 2*np.pi *wave.wavelength
            ampl *= nextamp
            opd  *= nextopd


        phasor = ampl * np.exp(1.j * 2* np.pi * opd/wave.wavelength)
        # and just hand back the last one to the calling routine:
        #return self.opticslist[-1].getPhasor(wave)
        return phasor


#------ Detector ------

class Detector(OpticalElement):
    """ A Detector is a specialized type of OpticalElement that forces a wavefront
    onto a specific fixed pixelization.

    Parameters
    ----------
    name : string
        Descriptive name
    pixelscale : float
        Pixel scale in arcsec/pixel
    fov_pixels, fov_arcsec : float
        The field of view may be specified either in arcseconds or by a number of pixels. Either is acceptable
        and the pixel scale is used to convert as needed. You may specify a non-square FOV by providing two elements in an iterable.
        Note that this follows the usual Python convention of ordering axes (Y,X), so put your desired Y axis size first. 
    oversample : int
        Oversampling factor beyond the detector pixel scale
    offset : tuple (X,Y)
        Offset for the detector center relative to a hypothetical off-axis PSF. Specifying this lets you
        pick a different sub-region for the detector to compute, if for some reason you are computing a small
        subarray around an off-axis source. (Has not been tested!)

    """
    def __init__(self, pixelscale, fov_pixels=None, fov_arcsec=None, oversample=1, name="Detector", offset=None, **kwargs):

        OpticalElement.__init__(self,name=name, planetype=_DETECTOR, **kwargs)
        self.pixelscale = float(pixelscale)
        self.oversample = oversample

        if fov_pixels is None and fov_arcsec is None:
            raise ValueError("Either fov_pixels or fov_arcsec must be specified!")
        elif fov_pixels is not None:
            self.fov_pixels = np.round(fov_pixels)
            self.fov_arcsec = self.fov_pixels * self.pixelscale
        else:
            # set field of view to closest value possible to requested,
            # consistent with having an integer number of pixels
            self.fov_pixels = np.round(np.asarray(fov_arcsec) / self.pixelscale)
            self.fov_arcsec = self.fov_pixels * self.pixelscale
        #if not np.isscalar(self.fov_pixels): # hasattr(self.fov_pixels, '__len__'):
            #self.shape = self.fov_pixels[0:2]  # rectangular
        #else:
            #self.shape = (self.fov_pixels, self.fov_pixels) # square


        self.amplitude = 1
        self.opd = 0

    @property
    def shape(self):
        return (self.fov_pixels, self.fov_pixels) if np.isscalar(self.fov_pixels) else self.fov_pixels[0:2]

    def __str__(self):
        return "Detector plane: %s (%dx%d, %f arcsec/pixel)" % (self.name, self.shape[1], self.shape[0], self.pixelscale)


#------  Optical System classes -------

class OpticalSystem():
    """ A class representing a series of optical elements,
    either Pupil, Image, or Detector planes, through which light
    can be propagated.

    The difference between
    Image and Detector planes is that Detectors have fixed pixels
    in terms of arcsec/pixel regardless of wavelength (computed via
    MFT) while Image planes have variable pixels scaled in terms of
    lambda/D. Pupil planes are some fixed size in meters, of course.

    Parameters
    ----------
    name : string
        descriptive name of optical system
    oversample : int
        Either how many times *above* Nyquist we should be
        (for pupil or image planes), or how many times a fixed
        detector pixel will be sampled. E.g. `oversample=2` means
        image plane sampling lambda/4*D (twice Nyquist) and
        detector plane sampling 2x2 computed pixels per real detector
        pixel.  Default is 2.
    verbose : bool
        whether to print stuff while computing




    """
    def __init__(self, name="unnamed system", verbose=True, oversample=2):
        self.name = name
        self.verbose=verbose
        self.planes = []                    # List of OpticalElements
        self.oversample = oversample

        self.source_offset_r = 0 # = np.zeros((2))     # off-axis tilt of the source, in ARCSEC
        self.source_offset_theta = 0 # in degrees CCW

        self.intermediate_wfs = None        #
        if self.verbose:
            _log.info("Initialized OpticalSystem: "+self.name)

    # Methods for adding or manipulating optical planes:

    def addPupil(self, optic=None, function=None, **kwargs):
        """ Add a pupil plane optic from file(s) giving transmission or OPD

          1) from file(s) giving transmission and/or OPD
                [set arguments `transmission=filename` and/or `opd=filename`]
          2) from an analytic function
                [set `function='Circle', 'Square'`
                and set additional kwargs to define shape etc.
          3) from an already-created OpticalElement object
                [set `optic=that object`]

        Parameters
        ----------
        optic : poppy.OpticalElement, optional
            An already-created OpticalElement object you would like to add
        function: string, optional
            The name of some analytic function you would like to use.
            Optional `kwargs` can be used to set the parameters of that function.
            Allowable function names are Circle, Square, Hexagon, Rectangle, and FQPM_FFT_Aligner
        opd, transmission : string, optional
            Filenames of FITS files describing the desired optic.


        Note: Now you can use the optic argument for either an OpticalElement or a string function name,
        and it will do the right thing depending on type.  Both existing arguments are left for compatibility for now.


        Any provided parameters are passed to :ref:`OpticalElement`.


        """
        if optic is None and function is not None: # ease of use: 'function' input and providing 'optic' parameter as a string are synonymous.
            optic = function


        if isinstance(optic, OpticalElement):
            # OpticalElement object provided. 
            # We can use it directly, but make sure the plane type is set.
            optic.planetype = _PUPIL
        elif isinstance(optic, str):
            # convenience code to instantiate objects from a string name.
            fns = {'circle':CircularAperture, 'square':SquareAperture, 'rectangle':RectangleAperture, 'rect':RectangleAperture, 
                    'hexagon': HexagonAperture, 'hex':HexagonAperture, 'fqpm_aligner':FQPM_FFT_aligner, 'fqpm_fft_aligner': FQPM_FFT_aligner}

            try:
                fn = fns[optic.lower()]
            except:
                raise ValueError("Unknown pupil function type: %s. Perhaps you meant to set transmission= or opd= instead?" % optic)
            optic = fn(oversample=self.oversample, **kwargs)
            optic.planetype = _PUPIL

        elif optic is None: 
            # create image from files specified in kwargs
            optic = FITSOpticalElement(planetype=_PUPIL, oversample=self.oversample, **kwargs)
        else:
            raise TypeError("Not sure how to handle an Optic input of that type...")

        self.planes.append(optic)
        if self.verbose: _log.info("Added pupil plane: "+self.planes[-1].name)

    def addImage(self, optic=None, function=None, **kwargs):
        """ Add an image plane optic to the optical system

        That image plane optic can be specified either

          1) from file(s) giving transmission or OPD
                [set arguments `transmission=filename` and/or `opd=filename`]
          2) from an analytic function
                [set `function='circle, fieldstop, bandlimitedcoron, or FQPM'`
                and set additional kwargs to define shape etc.
          3) from an already-created OpticalElement object
                [set `optic=that object`]

        Parameters
        ----------
        optic : poppy.OpticalElement
            An already-created OpticalElement you would like to add
        function: string
            Name of some analytic function to add.
            Optional `kwargs` can be used to set the parameters of that function.
            Allowable function names are CircularOcculter, fieldstop, BandLimitedCoron, FQPM
        opd, transmission : string
            Filenames of FITS files describing the desired optic.



        Notes
        ------

        Now you can use the optic argument for either an OpticalElement or a
        string function name, and it will do the right thing depending on type.
        Both existing arguments are left for back compatibility for now.



        """

        if isinstance(optic, str):
            function = optic
            optic = None

        if optic is None:
            if function == 'CircularOcculter':
                fn = IdealCircularOcculter
            elif function == 'BarOcculter':
                fn = IdealBarOcculter
            elif function == 'fieldstop':
                fn = IdealFieldStop
            elif function == 'BandLimitedCoron':
                fn = BandLimitedCoron
            elif function == 'FQPM':
                fn = IdealFQPM
            elif function is not None:
                raise ValueError("Analytic mask type '%s' is unknown." % function)
            else: # create image from files specified in kwargs
                fn = FITSOpticalElement

            optic = fn(oversample=self.oversample, **kwargs)
            optic.planetype=_IMAGE
        else:
            optic.planetype = _IMAGE
            optic.oversample = self.oversample # these need to match...

        self.planes.append(optic)
        if self.verbose: _log.info("Added image plane: "+self.planes[-1].name)

    def addRotation(self, *args, **kwargs):
        """ Add a clockwise or counterclockwise rotation around the optical axis """

        self.planes.append(Rotation(*args, **kwargs))
        if self.verbose: _log.info("Added rotation plane: "+self.planes[-1].name)


    def addDetector(self, pixelscale, oversample=None, **kwargs):
        """ Add a Detector object to an optical system.
        By default, use the same oversampling as the rest of the optical system,
        but the user can override to a different value if desired by setting `oversample`.


        Other arguments are passed to the init method for Detector().

        Parameters
        ----------
        pixelscale : float
            Pixel scale in arcsec/pixel
        oversample : int, optional
            Oversampling factor for *this detector*, relative to hardware pixel size.
            Optionally distinct from the default oversampling parameter of the OpticalSystem.

        """

        if oversample is None:
            oversample = self.oversample
        self.planes.append(Detector(pixelscale, oversample=oversample, **kwargs))
        if self.verbose: _log.info("Added detector: %s, with pixelscale=%f arcsec/pixel and oversampling=%d" % (self.planes[-1].name,pixelscale,oversample) )


        #return "Optical system '%s' containing %d optics" % (self.name, len(self.planes))

    def describe(self):
        """ Print out a string table describing all planes in an optical system"""
        print str(self)+"\n\t"+ "\n\t".join([str(p) for p in self.planes])

    def __getitem__(self, num):
        return self.planes[num]

    # methods for dealing with wavefronts:
    def inputWavefront(self, wavelength=2e-6):
        """Create a Wavefront object suitable for sending through a given optical system, based on
        the size of the first optical plane, assumed to be a pupil.

        If the first optical element is an Analytic pupil (i.e. has no pixel scale) then
        an array of 1024x1024 will be created (not including oversampling).

        Uses self.source_offset to assign an off-axis tilt, if requested.

        Parameters
        ----------
        wavelength : float
            Wavelength in meters

        Returns
        -------
        wavefront : poppy.Wavefront instance
            A wavefront appropriate for passing through this optical system.

        """

        npix = self.planes[0].shape[0] if self.planes[0].shape is not None else 1024
        diam = self.planes[0].pupil_diam if hasattr(self.planes[0], 'pupil_diam') else 8

        inwave = Wavefront(wavelength=wavelength,
                npix = npix,
                diam = diam,
                oversample=self.oversample)
        _log.debug("Creating input wavefront with wavelength=%f, npix=%d, pixel scale=%f meters/pixel" % (wavelength, npix, diam/npix))

        if np.abs(self.source_offset_r) > 0:
            offset_x = self.source_offset_r *-np.sin(self.source_offset_theta*np.pi/180)  # convert to offset X,Y in arcsec
            offset_y = self.source_offset_r * np.cos(self.source_offset_theta*np.pi/180)  # using the usual astronomical angle convention
            inwave.tilt(Xangle=offset_x, Yangle=offset_y)
            _log.debug("Tilted input wavefront by theta_X=%f, theta_Y=%f arcsec" % (offset_x, offset_y))
        return inwave

    def propagate_mono(self, wavelength=2e-6, normalize='first', save_intermediates=False, display_intermediates=False, intermediate_fn='wave_step_%03d.fits', poly_weight=None, **kwargs):
        """ Propagate a monochromatic wavefront through the optical system. Called from within calcPSF.
        Returns a fits.HDUList object.

        Parameters
        ----------
        wavelength : float
            Wavelength in meters
        normalize : string, {'first', 'last'}
            how to normalize the wavefront?
            * 'first' = set total flux = 1 after the first optic, presumably a pupil
            * 'last' = set total flux = 1 after the entire optical system.
            * 'first=2' = set total flux = 2 after the first optic (used for debugging only)
        display_intermediates : bool
            Should intermediate steps in the calculation be displayed on screen? Default False
        save_intermediates : bool
            Should intermediate steps in the calculation be saved to disk? Default False.
            If this is True, then setting `poly_weight` controls whether intermediate optical planes are actually saved to *disk* by this routine
            (for the monochromatic case) or are passed back up via memory and handled in `calcPSF` (for the polychromatic case).
        poly_weight : float
            is this being called as part of a polychromatic calculation?
            if not, set this to None. if so, set this to the weight for
            that wavelength. (This is used only for properly normalizing the
            multiwavelength FITs file written to disk if save_intermediates is set.)


        """

        if settings.enable_speed_tests():
            t_start = time.time()
        if self.verbose: _log.info(" Propagating wavelength = %g meters %s" % (wavelength, "" if poly_weight is None else " with weight=%.2f" % poly_weight))
        wavefront = self.inputWavefront(wavelength)

        if save_intermediates and poly_weight is None:
            self.intermediate_wfs=[]
            _log.warning("User requested saving intermediates, but no weight for this wavelength is provided - cannot properly calculate intermediate wavefront")
            _log.debug("reset intermediates")

        # do the propagation:
        #if display_intermediates:
            #suptitle = plt.suptitle( "propagating $\lambda=$ %.3f $\mu$m" % (wavelength*1e6), size='x-large')

            #wavefront.display(what='best',nrows=nrows,row=1, colorbar=False, title="propagating $\lambda=$ %.3f $\mu$m" % (wavelength*1e6))

        count = 0
        for optic in self.planes:
            wavefront.propagateTo(optic)
            wavefront *= optic
            count += 1


            if count==1 and normalize.lower()=='first':  # set entrance plane to 1. 
                wavefront.normalize()
            elif count==1 and normalize.lower()=='first=2': # this undocumented option is present only for testing/validation purposes
                wavefront.normalize()
                wavefront *= np.sqrt(2) 



            if settings.enable_flux_tests(): _log.debug("  Flux === "+str(wavefront.totalIntensity))

            if save_intermediates:
                if len(self.intermediate_wfs) < count:
                    self.intermediate_wfs.append(wavefront.copy())
                else:
                    self.intermediate_wfs[count-1] += wavefront.copy()*poly_weight
                    _log.info("    Storing intermediate wavefront plane %d with weight %f" % (count-1, poly_weight))
                if poly_weight is None: self.intermediate_wfs[count-1].writeto(intermediate_fn % count, what='parts')
            if display_intermediates:
                if settings.enable_speed_tests():
                    t0 = time.time()
                #if save_intermediates: self.intermediate_wfs[count-1].display(what='best',nrows=len(self.planes),row=count)
                title = None if count > 1 else "propagating $\lambda=$ %.3f $\mu$m" % (wavelength*1e6)
                wavefront.display(what='best',nrows=len(self.planes),row=count, colorbar=False, title=title)
                #plt.title("propagating $\lambda=$ %.3f $\mu$m" % (wavelength*1e6))

                if settings.enable_speed_tests():
                    t1 = time.time()
                    _log.debug("\tTIME %f s\t for displaying the wavefront." % (t1-t0))

        #if display_intermediates:
            #suptitle.remove() #  does not work due to some matplotlib limitation, so work around:
            #suptitle.set_text('') # clean up before next iteration to avoid ugly overwriting

        # prepare output arrays
        if normalize.lower()=='last':
                wavefront.normalize()

        if settings.enable_speed_tests():
            t_stop = time.time()
            _log.debug("\tTIME %f s\tfor propagating one wavelength" % (t_stop-t_start))

        return wavefront.asFITS()  # this returns the intensity, by default. 


    def calcPSF(self, wavelength=1e-6, weight=None,
        save_intermediates=False, save_intermediates_what='all', display= False, return_intermediates=False, source=None, **kwargs):
        """Calculate a PSF, either multi-wavelength or monochromatic.

        The wavelength coverage computed will be:
        - multi-wavelength PSF over some weighted sum of wavelengths (if you provide a source parameter)
        - monochromatic (if you provide just a wavelen= parameter)

        Any additional `kwargs` will be passed on to `propagate_mono()`

        Parameters
        ----------
        source : dict
            a dict containing 'wavelengths' and 'weights' list.
            *TBD - replace w/ pysynphot observation object*
        wavelen : float, optional
            wavelength in meters for monochromatic calculation.
        save_intermediates : bool
            whether to output intermediate optical planes to disk. Default is False
        save_intermediate_what : string
            What to save - phase, intensity, amplitude, complex, parts, all. default is all.
        return_intermediates: bool
            return intermediate wavefronts as well as PSF?
        display : bool
            whether to display when finished or not.


        Returns
        -------
        outfits :
            a fits.HDUList
        """

        tstart = time.time() 
        if source is not None:
            wavelength = source['wavelengths']
            weight=source['weights']

        if not hasattr(wavelength,'__iter__'):
            wavelength = [wavelength]
        if weight is None:
            weight = [1.0] * len(wavelength)

        if len(tuple(wavelength)) != len(tuple(weight)):
            raise ValueError("Input source has different number of weights and wavelengths...")

        if display: plt.clf()

        if save_intermediates or return_intermediates:
            self.intermediate_wfs = []
            _log.info("User requested saving intermediate wavefronts in call to poppy.calcPSF")
            _log.debug('reset intermediates in calcPSF')
            #raise ValueError("Saving intermediates for multi-wavelen not yet implemented!!")
        else:
            self.intermediate_wfs = None

        # loop over wavelengths
        if self.verbose: _log.info("Calculating PSF with %d wavelengths" % (len(wavelength)))
        outFITS = None

        normwts =  np.asarray(weight, dtype=float)
        normwts /= normwts.sum()

        if settings.use_fftw():
            utils.fftw_load_wisdom()


        if settings.use_multiprocessing() and len(wavelength) > 1 : ######### Parallellized computation ############
            if settings.use_fftw(): 
                _log.warn('IMPORTANT WARNING: Python multiprocessing and fftw3 do not appear to play well together. This may crash intermittently')
                _log.warn('   We suggest you set   poppy.settings.use_fftw to False   if you want to use multiprocessing().')
            if display:
                _log.warn('Display during calculations is not supported for multiprocessing mode. Please set poppy.settings.use_multiprocessing.set(False) if you want to use display=True.')
                _log.warn('For now, display is being set to False.')
                display=False

            if save_intermediates:
                raise NotImplementedError("Can't save intermediate steps if using parallelized code")
                save_intermediates = False
                self.intermediate_wfs = []

            # do *NOT* just blindly try to create as many processes as one has CPUs, or one per wavelength either
            # This is a memory-intensive task so that can end up swapping to disk and thrashing IO

            if settings.n_processes() < 1:
                nproc = utils.estimate_optimal_nprocesses(self, nwavelengths=len(wavelength))
            else:
                nproc = settings.n_processes()
            pool = multiprocessing.Pool( int(nproc) )  # be sure to cast to int, will fail if given a float even if of integer value


            # build a single iterable containing the required function arguments
            _log.info("Beginning multiprocessor job using {0} processes".format(nproc))
            iterable = [(self, wavelen, wt, kwargs, settings.use_fftw()) for wavelen, wt in zip(wavelength, normwts)]
            results = pool.map(_wrap_propagate_for_multiprocessing, iterable)
            _log.info("Finished multiprocessor job")
            pool.close()

            # Sum all the results up into one array, using the weights
            outFITS = results[0]
            outFITS[0].data *= normwts[0]
            _log.info("got results for wavelength channel %d / %d" % (0, len(tuple(wavelength))) )
            for i in range(1, len(normwts)):
                _log.info("got results for wavelength channel %d / %d" % (i, len(tuple(wavelength))) )
                outFITS[0].data += results[i][0].data * normwts[i]
            outFITS[0].header.add_history("Multiwavelength PSF calc on %d processors completed." % settings.n_processes())




        else:  ########## single-threaded computations (may still use multi cores if FFTW enabled ######


            for wavelen, wave_weight in zip(wavelength, normwts):
            #for wavelen, weight in zip(source['wavelengths'], normwts):
                mono_psf = self.propagate_mono(wavelen, poly_weight=wave_weight, save_intermediates=save_intermediates or return_intermediates, **kwargs)
                # add mono_psf into the output array:

                if outFITS is None:
                    outFITS = mono_psf
                    outFITS[0].data = mono_psf[0].data*wave_weight
                else:
                    outFITS[0].data += mono_psf[0].data *wave_weight

            if save_intermediates:
                for i in range(len(self.intermediate_wfs)):
                    self.intermediate_wfs[i].writeto('wavefront_plane_%03d.fits' % i, what=save_intermediates_what, **kwargs )

        tstop = time.time()
        tdelta = tstop-tstart
        _log.info("  Calculation completed in {0:.3f} s".format(tdelta))
        outFITS[0].header.add_history("Calculation completed in {0:.3f} seconds".format(tdelta))



        if display:
            # This display may be redundant if display_intermediates is not already set...
            cmap = matplotlib.cm.jet
            cmap.set_bad('0.3')
            #cmap.set_bad('k', 0.8)
            halffov_x =outFITS[0].header['PIXELSCL']*outFITS[0].data.shape[1]/2
            halffov_y =outFITS[0].header['PIXELSCL']*outFITS[0].data.shape[0]/2
            extent = [-halffov_x, halffov_x, -halffov_y, halffov_y]
            unit="arcsec"
            norm=LogNorm(vmin=1e-8,vmax=1e-1)
            plt.xlabel(unit)

            utils.imshow_with_mouseover(outFITS[0].data, extent=extent, norm=norm, cmap=cmap)



        if settings.use_fftw() and settings.autosave_fftw_wisdom():
            utils.fftw_save_wisdom()

        # TODO update FITS header for oversampling here if detector is different from regular?
        waves = np.asarray(wavelength)
        wts = np.asarray(weight)
        mnwave = (waves*wts).sum() / wts.sum()
        outFITS[0].header.update('WAVELEN', mnwave, 'Weighted mean wavelength in meters')
        outFITS[0].header.update('NWAVES',waves.size, 'Number of wavelengths used in calculation')
        for i in range(waves.size):
            outFITS[0].header.update('WAVE'+str(i), waves[i], "Wavelength "+str(i))
            outFITS[0].header.update('WGHT'+str(i), wts[i], "Wavelength weight "+str(i))
        if settings.use_fftw():
            ffttype = "pyFFTW"
        else:
            ffttype = "numpy.fft"
        outFITS[0].header.update('FFTTYPE',ffttype, 'Algorithm for FFTs: numpy or fftw')

        if self.verbose: _log.info("PSF Calculation completed.")
        if return_intermediates:
            return outFITS, self.intermediate_wfs
        else:
            return outFITS


    def display(self, **kwargs):
        """ Display all elements in an optical system on screen.

        Any extra arguments are passed to the `optic.display()` methods of each element.

        """

        planes_to_display = [p for p in self.planes if (not isinstance(p, Detector) and not p._suppress_display)]
        nplanes = len(planes_to_display)
        for i, plane in enumerate(planes_to_display):
            _log.info("Displaying plane %sin row %d of %d" % (plane.name, i+1, nplanes))
            plane.display(nrows=nplanes, row=i+1, **kwargs)


    def _propagation_info(self):
        """ Provide some summary information on the optical propagation calculations that
        would be done for a given optical system 

        Right now this mostly is checking whether a given propagation makes use of FFTs or not,
        since the padding for oversampled FFTS majorly affects the max memory used for multiprocessing
        estimation """

        steps = []
        for i, p in enumerate(self.planes):
            if i == 0: continue # no propagation needed for first plane
            if p.planetype == _ROTATION:  steps.append('rotation')
            elif self.planes[i-1].planetype==_PUPIL and p.planetype ==_DETECTOR: steps.append('MFT')
            elif self.planes[i-1].planetype==_PUPIL and p.planetype ==_IMAGE:
                  if i > 1 and steps[-1] =='MFT': steps.append('invMFT')
                  else: steps.append('FFT')
            elif self.planes[i-1].planetype==_IMAGE and p.planetype == _DETECTOR: steps.append('resample')
            else: steps.append('FFT')

        
        output_shape = [a * self.planes[-1].oversample for a in self.planes[-1].shape]
        output_size = output_shape[0]*output_shape[1]

        return {'steps': steps, 'output_shape': output_shape, 'output_size':output_size}


class SemiAnalyticCoronagraph(OpticalSystem):
    """ A subclass of OpticalSystem that implements a specialized propagation
    algorithm for coronagraphs whose occulting mask has limited and small support in
    the image plane. Algorithm from Soummer et al. (2007)

    The way to use this class is to build an OpticalSystem class the usual way, and then
    cast it to a SemiAnalyticCoronagraph, and then you can just call calcPSF on that in the
    usual fashion.

    Parameters
    -----------
    ExistingOpticalSystem : OpticalSystem
        An optical system which can be converted into a SemiAnalyticCoronagraph. This
        means it must have exactly 4 planes, in order Pupil, Image, Pupil, Detector.
    oversample : int
        Oversampling factor in intermediate image plane. Default is 8
    occulter_box : float
        half size of field of view region entirely including the occulter, in arcseconds. Default 1.0
        This can be a tuple or list to specify a rectangular region [deltaY,deltaX] if desired.


    Notes
    ------

    Note that this algorithm is only appropriate for certain types of Fourier transform,
    namely those using occulters limited to a sub-region of the image plane.
    It is certainly appropriate for TFI, and probably the right choice for NIRCam as well, but
    is of no use for MIRI's FQPMs.



    """

    def __init__(self, ExistingOpticalSystem, oversample=8, occulter_box = 1.0):

        if len(ExistingOpticalSystem.planes) != 4:
            raise ValueError("Input optical system must have exactly 4 planes to be convertible into a SemiAnalyticCoronagraph")
        self.name = "SemiAnalyticCoronagraph for "+ExistingOpticalSystem.name
        self.verbose = ExistingOpticalSystem.verbose
        self.source_offset_r = ExistingOpticalSystem.source_offset_r
        self.source_offset_theta = ExistingOpticalSystem.source_offset_theta
        self.planes = ExistingOpticalSystem.planes

        # SemiAnalyticCoronagraphs have some fixed planes, so give them reasonable names.
        self.inputpupil = self.planes[0]
        self.occulter = self.planes[1]
        self.lyotplane = self.planes[2]
        self.detector = self.planes[3]

        self.mask_function = InverseTransmission(self.occulter)

        for i, typecode in enumerate([_PUPIL, _IMAGE, _PUPIL, _DETECTOR]):
            if not self.planes[i].planetype == typecode:
                raise ValueError("Plane %i is not of the right type for a semianalytic coronagraph calculation: should be %s but is %s." % (i, _typestrs[typecode], _typestrs[self.planes[i].planetype]))


        self.oversample = oversample

        if hasattr(occulter_box, '__getitem__'):
            occulter_box = np.array(occulter_box) # cast to numpy array so the multiplication by 2 just below will work
        self.occulter_box = occulter_box

        self.occulter_det = Detector(self.detector.pixelscale/self.oversample, fov_arcsec = self.occulter_box*2, name='Oversampled Occulter Plane')

    def propagate_mono(self, wavelength=2e-6, normalize='first', save_intermediates=False, display_intermediates=False, intermediate_fn=None, poly_weight=None):
        """

        Parameters
        -------------
        wavelength : float
            Wavelength in meters
        normalize : string, {'first', 'last'}
            how to normalize the wavefront?

            * 'first' : set total flux = 1 after the first optic, presumably a pupil
            * 'last' : set total flux = 1 after the entire optical system.


        save_intermediates, display_intermediates, intermediate_fn, poly_weight : bools
            Ignored in current version of code?

        """
        if settings.enable_speed_tests():
            t_start = time.time()
        if self.verbose: _log.info(" Propagating wavelength = %g meters" % wavelength)
        wavefront = self.inputWavefront(wavelength)

        if save_intermediates:
            raise NotImplemented("not yet")

        #------- differences from regular propagation begin here --------------
        wavefront *= self.inputpupil

        if normalize.lower()=='first':
            wavefront.normalize()


        if display_intermediates:
            #suptitle = plt.suptitle( "propagating $\lambda=$ %.3f $\mu$m" % (wavelength*1e6), size='x-large')

            nrows = 6
            #plt.clf()
            wavefront.display(what='best',nrows=nrows,row=1, colorbar=False, title="propagating $\lambda=$ %.3f $\mu$m" % (wavelength*1e6))


        # determine FOV region bounding the image plane occulting stop.
        # determine number of pixels across that to use ("N_B")
        # calculate the MFT to the N_B x N_B occulting region.
        wavefront_cor = wavefront.copy()
        wavefront_cor.propagateTo(self.occulter_det)

        if display_intermediates: wavefront_cor.display(what='best',nrows=nrows,row=2, colorbar=False)


        # Multiply that by M(r) =  1 - the occulting plane mask function
        wavefront_cor *= self.mask_function

        if display_intermediates: wavefront_cor.display(what='best',nrows=nrows,row=3, colorbar=False)

        # calculate the MFT from that small region back to the full Lyot plane

        wavefront_lyot = wavefront_cor.copy()
        wavefront_lyot.propagateTo(self.lyotplane)

        if display_intermediates: wavefront_lyot.display(what='best',nrows=nrows,row=4, colorbar=False)
        # combine that with the original pupil function
        wavefront_combined = wavefront + (-1)*wavefront_lyot

        wavefront_combined *= self.lyotplane
        wavefront_combined.location = 'after combined Lyot pupil'

        if display_intermediates: wavefront_combined.display(what='best',nrows=nrows,row=5, colorbar=False)

        # propagate to the real detector in the final image plane.
        wavefront_combined.propagateTo(self.detector)

        if display_intermediates: 
            wavefront_combined.display(what='best',nrows=nrows,row=6, colorbar=False)
            #suptitle.remove() #  does not work due to some matplotlib limitation, so work arount:
            suptitle.set_text('') # clean up before next iteration to avoid ugly overwriting



        #------- differences from regular propagation end here --------------

        # prepare output arrays
        if normalize.lower()=='last':
                wavefront_combined.normalize()

        if settings.enable_speed_tests():
            t_stop = time.time()
            _log.debug("\tTIME %f s\tfor propagating one wavelength" % (t_stop-t_start))

        return wavefront_combined.asFITS()



if __name__ == "__main__":
    import pylab as pl
    logging.basicConfig(level=logging.INFO,format='%(name)-10s: %(levelname)-8s %(message)s')

    if False:
        for npix in (111,112):
        #for offset in [ (0,0)]:
            for offset in [ (0,0), (0,0.5), (1,0), (1,1.5)]:

                osys = OpticalSystem("Circle JW", oversample=1)
                #osys.addPupil(function='Square', name = "6.5m square", size=6.5)
                osys.addPupil(function='Circle', name = "6.5m circle", radius=6.5/2)
                #osys.addImage()
                #osys.addDetector(0.032, name="Detector", fov_pixels=128)
                osys.addDetector(0.032/2, name="Detector", fov_pixels=npix)

                osys.planes[-1].det_offset = offset


                src = {'wavelengths': [2e-6], 'weights': [1.0]}
                res = osys.calcPSF(src, display=True)


                #plt.clf()
                #plt.imshow(res[0].data)
                res.writeto('test_ci_np%d_off%s.fits' %(npix, offset), clobber=True)

    #test_speed()
