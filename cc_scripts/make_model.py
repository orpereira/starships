from sys import path

import os
import sys

from pathlib  import Path
import numpy as np
import yaml

import logging
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
logging.basicConfig()

import emcee
import starships.spectrum as spectrum
from starships.orbite import rv_theo_t
from starships.mask_tools import interp1d_masked

interp1d_masked.iprint = False
import starships.correlation as corr
from starships.analysis import bands, resamp_model
import starships.planet_obs as pl_obs
from starships.planet_obs import Observations, Planet
import starships.petitradtrans_utils as prt
from starships.homemade import unpack_kwargs_from_command_line
from starships import retrieval_utils as ru

from starships.instruments import load_instrum


import astropy.units as u
import astropy.constants as const
from astropy.table import Table

from scipy.interpolate import interp1d

import warnings

warnings.simplefilter("ignore", UserWarning)
warnings.simplefilter("ignore", RuntimeWarning)

import gc

# from petitRADTRANS import nat_cst as nc
try:
    from petitRADTRANS.physics import guillot_global, guillot_modif
except ModuleNotFoundError:
    from petitRADTRANS.nat_cst import guillot_global, guillot_modif



def init_model_retrieval(config_dict, mol_species=None, kind_res='high', lbl_opacity_sampling=None,
                         wl_range=None, continuum_species=None, pressures=None, **kwargs):
    """
    Initialize some objects needed for modelization: atmo, species, star_fct, pressures
    :param mol_species: list of species included (without continuum opacities)
    :param kind_res: str, 'high' or 'low'
    :param lbl_opacity_sampling: ?
    :param wl_range: wavelength range (2 elements tuple or list, or None)
    :param continuum_species: list of continuum opacities, H and He excluded
    :param pressures: pressure array. Default is `default_params['pressures']`
    :param kwargs: other kwargs passed to `starships.petitradtrans_utils.select_mol_list()`
    :return: atmos, species, star_fct, pressure array
    """

    if mol_species is None:
        mol_species = config_dict['line_opacities']

    if lbl_opacity_sampling is None:
        lbl_opacity_sampling = config_dict['opacity_sampling']

    if continuum_species is None:
        continuum_species = config_dict['continuum_opacities']

    if pressures is None:
        limP = config_dict['limP']
        n_pts = config_dict['n_pts']
        pressures = np.logspace(*limP, n_pts)

    species = prt.select_mol_list(mol_species, kind_res=kind_res, **kwargs)
    species_2_lnlst = {mol: lnlst for mol, lnlst in zip(mol_species, species)}

    if kind_res == 'high':
        mode = 'lbl'
        Raf = load_instrum(config_dict['instrument'])['resol']
        pix_per_elem = 2
        if wl_range is None:
            wl_range = load_instrum(config_dict['instrument'])['high_res_wv_lim']

    elif kind_res == 'low':
        mode = 'c-k'
        Raf = 1000
        pix_per_elem = 1
        if wl_range is None:
            wl_range = config_dict['intrument']['low_res_wv_lim']
    else:
        raise ValueError(f'`kind_res` = {kind_res} not valid. Choose between high or low')


    atmo, _ = prt.gen_atm_all([*species.keys()], pressures, mode=mode,
                                      lbl_opacity_sampling=lbl_opacity_sampling, wl_range=wl_range,
                                      continuum_opacities=continuum_species)

    # --- downgrading the star spectrum to the wanted resolution
    if config_dict['kind_trans'] == 'emission' and config_dict['star_wv'] is not None:
        resamp_star = np.ma.masked_invalid(
            resamp_model(config_dict['star_wv'][(config_dict['star_wv'] >= wl_range[0] - 0.1) & (config_dict['star_wv'] <= wl_range[1] + 0.1)],
                         config_dict['star_flux'][(config_dict['star_wv'] >= wl_range[0] - 0.1) & (config_dict['star_wv'] <= wl_range[1] + 0.1)], 500000, Raf=Raf,
                         pix_per_elem=pix_per_elem))
        fct_star = interp1d(config_dict['star_wv'][(config_dict['star_wv'] >= wl_range[0] - 0.1) & (config_dict['star_wv'] <= wl_range[1] + 0.1)],
                                     resamp_star)
    else:
        fct_star = None

    return atmo, species_2_lnlst, fct_star


def prepare_abundances(config_dict, mode=None, ref_linelists=None):
    """Use the correct linelist name associated to the species."""

    if ref_linelists is None:
        if mode is None:
            ref_linelists = config_dict['line_opacities'].copy()
        else:
            ref_linelists = [config_dict['linelist_names'][mode][mol] for mol in config_dict['line_opacities']]

    theta_dict = {}
    if config_dict['chemical_equilibrium']:
        for mol in config_dict['line_opacities']:
            theta_dict[mol] = 10 ** (-99.0)

    # --- Prepare the abundances (with the correct linelist name for species)
    species = {lnlst: theta_dict[mol] for lnlst, mol
               in zip(ref_linelists, config_dict['line_opacities'])}
    
    # --- Adding continuum opacities
    for mol in config_dict['continuum_opacities']:
        species[mol] = theta_dict[mol]
        
    # --- Adding other species
    for mol in config_dict['other_species']:
        species[mol] = theta_dict[mol]

    return species

def create_internal_dict(config_dict, planet):
    ''' for internally created variables to be used in other functions'''
    int_dict = {}

    limP = config_dict['limP']
    n_pts = config_dict['n_pts']
    int_dict['pressures'] = np.logspace(*limP, n_pts)

    # need to 
    int_dict['temperatures'] = config_dict['T_eq']* np.ones_like(int_dict['pressures'])

    int_dict['P0'] = config_dict['P0']
    int_dict['p_cloud'] = config_dict['p_cloud']
    int_dict['R_pl'] = planet.R_pl[0].to(u.R_jup).cgs.value
    int_dict['R_star'] = planet.R_star.to(u.R_sun).cgs.value
    int_dict['gravity'] = (const.G * planet.M_pl / (planet.R_pl) ** 2).cgs.value

    return int_dict

def prepare_model_high_or_low(config_dict, int_dict, planet, atmo_obj=None, fct_star=None,
                              species_dict=None, Raf=None, rot_ker=None):

    mode = config_dict['mode']
    if Raf is None:
        Raf = load_instrum(config_dict['instrument'])['resol']
    
    if atmo_obj is None:
        # Use atmo object in globals parameters if it exists
        atmo_obj = config_dict['atmo_high'] if mode == 'high' else config_dict['atmo_low']
        # Initiate if not done yet
        if atmo_obj is None:
            log.info(f'Model not initialized for mode = {mode}. Starting initialization...')
            output = init_model_retrieval(config_dict, kind_res=mode)
            log.info('Saving values in `linelist_names`.')
            atmo_obj, lnlst_names, config_dict['fct_star_global'][mode] = output
            # Update the values of the global variables
            # Need to use globals() otherwise an error is raised.
            if mode == 'high':
                globals()['atmo_high'] = atmo_obj
            else:
                globals()['atmo_low'] = atmo_obj
                
            # Update the line list names
            if config_dict['linelist_names'][mode] is None:
                config_dict['linelist_names'][mode] = lnlst_names
            else:
                # Keep the predefined values and complete with the new ones
                config_dict['linelist_names'][mode] = {**lnlst_names, **config_dict['linelist_names'][mode]}

    if fct_star is None:
        fct_star = config_dict['fct_star_global'][mode]

    # --- Prepare the abundances (with the correct name for species)
    # Note that if species is None (not specified), `linelist_names[mode]` will be used inside `prepare_abundances`.
    species = prepare_abundances(config_dict, mode, species_dict)

    # --- Generating the model
    args = [int_dict[key] for key in ['pressures', 'temperatures', 'gravity', 'P0', 'p_cloud', 'R_pl', 'R_star']]
    kwargs = dict(gamma_scat=config_dict['gamma_scat'],
                  kappa_factor=config_dict['scat_factor'],
                  C_to_O=config_dict['C/O'],
                    Fe_to_H=config_dict['Fe/H'],
                    specie_2_lnlst=config_dict['linelist_names'][mode],
                    kind_trans=config_dict['kind_trans'],
                    dissociation=config_dict['dissociation'],
                    fct_star=fct_star)
    wv_out, model_out = prt.retrieval_model_plain(atmo_obj, species, planet, *args, **kwargs)

    if mode == 'high':
        # --- Downgrading and broadening the model (if broadening is included)
        # if np.isfinite(model_out[100:-100]).all():
            # Get wind broadening parameters
            if config_dict['wind'] is not None:
                rot_kwargs = {'rot_params': [config_dict['R_pl'] * const.R_jup,
                                             config_dict['M_pl'],
                                             config_dict['T_eq'] * u.K,
                                             [config_dict['wind']]],
                                'gauss': True, 'x0': 0,
                                'fwhm': config_dict['wind'] * 1e3, }
            else:
                rot_kwargs = {'rot_params': None}
            
            # lbl_res = 1e6 / config_dict['opacity_sampling']
            lbl_res = 1000000

            # Downgrade the model
            wv_out, model_out = prt.prepare_model(wv_out, model_out, lbl_res, Raf=Raf,
                                                  rot_ker=rot_ker, **rot_kwargs)

    return wv_out, model_out