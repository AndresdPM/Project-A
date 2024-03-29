#!/usr/bin/env python

from __future__ import print_function

import argparse
import sys
import os
import subprocess
import warnings
import numpy as np
import pandas as pd
from math import log10, floor

import matplotlib.pyplot as plt
from sklearn import mixture
from scipy import stats

from astropy.table import Table
from astropy.io import fits
from astropy.wcs import WCS
from astropy.time import Time
from astropy.wcs.utils import proj_plane_pixel_scales
from astropy.visualization import (ImageNormalize, ManualInterval)
import astropy.units as u
from astropy.coordinates import SkyCoord


def round_significant(x, ex, sig=1):
   """
   This routine returns a quantity rounded to its error significan figures.
   """

   significant = sig-int(floor(log10(abs(ex))))-1

   return round(x, significant), round(ex, significant)


def manual_select_from_cmd(color, mag):
   """
   Select stars based on their membership probabilities and cmd position
   """
   from matplotlib.path import Path
   from matplotlib.widgets import LassoSelector

   class SelectFromCollection(object):
      """
      Select indices from a matplotlib collection using `LassoSelector`.
      """
      def __init__(self, ax, collection, alpha_other=0.15):
         self.canvas = ax.figure.canvas
         self.collection = collection
         self.alpha_other = alpha_other

         self.xys = collection.get_offsets()
         self.Npts = len(self.xys)

         # Ensure that we have separate colors for each object
         self.fc = collection.get_facecolors()
         if len(self.fc) == 0:
            raise ValueError('Collection must have a facecolor')
         elif len(self.fc) == 1:
            self.fc = np.tile(self.fc, (self.Npts, 1))

         lineprops = {'color': 'r', 'linewidth': 1, 'alpha': 0.8}
         self.lasso = LassoSelector(ax, onselect=self.onselect, lineprops=lineprops)
         self.ind = []

      def onselect(self, verts):
         path = Path(verts)
         self.ind = np.nonzero(path.contains_points(self.xys))[0]
         self.selection = path.contains_points(self.xys)
         self.fc[:, -1] = self.alpha_other
         self.fc[self.ind, -1] = 1
         self.collection.set_facecolors(self.fc)
         self.canvas.draw_idle()

      def disconnect(self):
         self.lasso.disconnect_events()
         self.fc[:, -1] = 1
         self.collection.set_facecolors(self.fc)
         self.canvas.draw_idle()

   subplot_kw = dict(autoscale_on = False)
   fig, ax = plt.subplots(subplot_kw = subplot_kw)

   pts = ax.scatter(color, mag, c = '0.2', s=1)

   try:
      ax.set_xlim(np.nanmin(color)-0.1, np.nanmax(color)+0.1)
      ax.set_ylim(np.nanmax(mag)+0.05, np.nanmin(mag)-0.05)
   except:
      pass

   ax.grid()
   ax.set_xlabel(color.name)
   ax.set_ylabel(mag.name)

   selector = SelectFromCollection(ax, pts)

   def accept(event):
      if event.key == "enter":
         selector.disconnect()
         plt.close('all')

   fig.canvas.mpl_connect("key_press_event", accept)
   ax.set_title("Use your cursor to select likely member stars and press enter to accept.")

   plt.show()

   input("Once your selection is made, please press enter to continue.")

   try:
      return selector.selection
   except:
      return [True]*len(mag)


def correct_flux_excess_factor(bp_rp, phot_bp_rp_excess_factor):
   """
   Calculate the corrected flux excess factor for the input Gaia EDR3 data.
   """

   if np.isscalar(bp_rp) or np.isscalar(phot_bp_rp_excess_factor):
       bp_rp = np.float64(bp_rp)
       phot_bp_rp_excess_factor = np.float64(phot_bp_rp_excess_factor)
   
   if bp_rp.shape != phot_bp_rp_excess_factor.shape:
       raise ValueError('Function parameters must be of the same shape!')
   
   do_not_correct = np.isnan(bp_rp)
   bluerange = np.logical_not(do_not_correct) & (bp_rp < 0.5)
   greenrange = np.logical_not(do_not_correct) & (bp_rp >= 0.5) & (bp_rp < 4.0)
   redrange = np.logical_not(do_not_correct) & (bp_rp > 4.0)
   
   correction = np.zeros_like(bp_rp)
   correction[bluerange] = 1.154360 + 0.033772*bp_rp[bluerange] + 0.032277*np.power(bp_rp[bluerange],2)
   correction[greenrange] = 1.162004 + 0.011464*bp_rp[greenrange] + 0.049255*np.power(bp_rp[greenrange],2) - 0.005879*np.power(bp_rp[greenrange],3)
   correction[redrange] = 1.057572 + 0.140537*bp_rp[redrange]
   
   return phot_bp_rp_excess_factor - correction


def get_real_error(table):
   """
   This routine calculates the excess of error that should be added to the listed Gaia errors. The lines are based on Fabricius 2020. Figure 21.
   """

   p5 = table.astrometric_params_solved == 31                                        # 5p parameters solved Brown et al. (2020)
   
   table.loc[:, ['parallax_error_old', 'pmra_error_old', 'pmdec_error_old']] = table.loc[:, ['parallax_error', 'pmra_error', 'pmdec_error']].values
   
   table.loc[p5, ['parallax_error', 'pmra_error', 'pmdec_error']] = np.sqrt(table.loc[p5, ['parallax_error', 'pmra_error', 'pmdec_error']].values**2 * 1.05**2)
   table.loc[~p5, ['parallax_error', 'pmra_error', 'pmdec_error']] = np.sqrt(table.loc[~p5, ['parallax_error', 'pmra_error', 'pmdec_error']].values**2 * 1.22**2)

   return table


def clean_astrometry(ruwe, ipd_gof_harmonic_amplitude, visibility_periods_used, astrometric_excess_noise_sig, astrometric_params_solved, use_5p = False):
   """
   Select stars with good astrometry in Gaia.
   """

   labels_ruwe = ruwe <= 1.4
   labels_harmonic_amplitude = ipd_gof_harmonic_amplitude <= 0.2                                    # Reject blended transits Fabricius et al. (2020)
   labels_visibility = visibility_periods_used >= 9                                                 # Lindengren et al. (2020)
   labels_excess_noise = astrometric_excess_noise_sig <= 2.0                                        # Lindengren et al. (2020)

   labels_astrometric = labels_ruwe & labels_harmonic_amplitude & labels_visibility & labels_excess_noise

   if use_5p:
      labels_params_solved = astrometric_params_solved == 31                                        # 5p parameters solved Brown et al. (2020)
      labels_astrometric = labels_astrometric & labels_params_solved

   return labels_astrometric


def clean_photometry(gmag, corrected_flux_excess_factor, sigma_flux_excess_factor = 3):
   """
   This routine select stars based on their flux_excess_factor. Riello et al.2020
   """
   from matplotlib.path import Path

   def sigma_corrected_C(gmag, sigma_flux_excess_factor):
      return sigma_flux_excess_factor*(0.0059898 + 8.817481e-12 * gmag ** 7.618399)

   mag_nodes = np.linspace(np.min(gmag)-0.1, np.max(gmag)+0.1, 100)
   
   up = [(gmag, sigma) for gmag, sigma in zip(mag_nodes, sigma_corrected_C(mag_nodes, sigma_flux_excess_factor))]
   down = [(gmag, sigma) for gmag, sigma in zip(mag_nodes[::-1], sigma_corrected_C(mag_nodes, -sigma_flux_excess_factor)[::-1])]

   path_C = Path(up+down, closed=True)
   
   labels_photometric = path_C.contains_points(np.array([gmag, corrected_flux_excess_factor]).T)

   return labels_photometric


def pre_clean_data(phot_g_mean_mag, corrected_flux_excess_factor, ruwe, ipd_gof_harmonic_amplitude, visibility_periods_used, astrometric_excess_noise_sig, astrometric_params_solved, sigma_flux_excess_factor = 3, use_5p = False):
   """
   This routine cleans the Gaia data from astrometrically and photometric bad measured stars.
   """
   
   labels_photometric = clean_photometry(phot_g_mean_mag, corrected_flux_excess_factor, sigma_flux_excess_factor = sigma_flux_excess_factor)
   
   labels_astrometric = clean_astrometry(ruwe, ipd_gof_harmonic_amplitude, visibility_periods_used, astrometric_excess_noise_sig, astrometric_params_solved, use_5p = use_5p)
   
   return labels_photometric & labels_astrometric


def remove_jobs():
   """
   This routine removes jobs from the Gaia archive server.
   """

   list_jobs = []
   for job in Gaia.list_async_jobs():
      list_jobs.append(job.get_jobid())
   
   Gaia.remove_jobs(list_jobs)


def gaia_query(Gaia, query, min_gmag, max_gmag, save_individual_queries, load_existing, name, n, n_total):
   """
   This routine launch the query to the Gaia archive.
   """

   query = query + " AND (phot_g_mean_mag > %.4f) AND (phot_g_mean_mag <= %.4f)"%(min_gmag, max_gmag)

   individual_query_filename = './%s/Gaia/individual_queries/%s_G_%.4f_%.4f.csv'%(name, name, min_gmag, max_gmag)

   if os.path.isfile(individual_query_filename) and load_existing:
      result = pd.read_csv(individual_query_filename)

   else:
      job = Gaia.launch_job_async(query)
      result = job.get_results()
      removejob = Gaia.remove_jobs([job.jobid])
      result = result.to_pandas()

      if save_individual_queries:
         result.to_csv(individual_query_filename, index = False)

   print('\n')
   print('----------------------------')
   print('Table %i of %i: %i stars'%(n, n_total, len(result)))
   print('----------------------------')

   return result, query


def get_mag_bins(min_mag, max_mag, area, mag = None):
   """
   This routine generates logarithmic spaced bins for G magnitude.
   """

   num_nodes = np.max((1, np.round( ( (max_mag - min_mag) * max_mag ** 2 * area)*5e-5)))

   bins_mag = (1.0 + max_mag - np.logspace(np.log10(1.), np.log10(1. + max_mag - min_mag), num = int(num_nodes), endpoint = True))

   return bins_mag


def gaia_multi_query_run(args):
   """
   This routine pipes gaia_query into multiple threads.
   """

   return gaia_query(*args)


def columns_n_conditions(source_table, astrometric_cols, photometric_cols, quality_cols, ra, dec, width = 1.0, height = 1.0):

   """
   This routine generates the columns and conditions for the query.
   """

   if 'dr3' in source_table:
      if 'ruwe' not in quality_cols:
         quality_cols = 'ruwe' +  (', ' + quality_cols if len(quality_cols) > 1 else '')
   elif 'dr2' in source_table:
      if 'astrometric_n_good_obs_al' not in quality_cols:
         quality_cols = 'astrometric_n_good_obs_al' +  (', ' + quality_cols if len(quality_cols) > 1 else '')
      if 'astrometric_chi2_al' not in quality_cols:
         quality_cols = 'astrometric_chi2_al' +  (', ' + quality_cols if len(quality_cols) > 1 else '')
      if 'phot_bp_rp_excess_factor' not in quality_cols:
         quality_cols = 'phot_bp_rp_excess_factor' +  (', ' + quality_cols if len(quality_cols) > 1 else '')

   conditions = "CONTAINS(POINT('ICRS',"+source_table+".ra,"+source_table+".dec),BOX('ICRS',%.8f,%.8f,%.8f,%.8f))=1"%(ra, dec, width, height)

   columns = (", " + astrometric_cols if len(astrometric_cols) > 1 else '') + (", " + photometric_cols if len(photometric_cols) > 1 else '') +  (", " + quality_cols if len(quality_cols) > 1 else '')

   query = "SELECT source_id " + columns + " FROM " + source_table + " WHERE " + conditions

   return query, quality_cols
   

def incremental_query(query, area, min_gmag = 10.0, max_gmag = 19.5, use_parallel = True, save_individual_queries = False, load_existing = False, name = 'output'):

   """
   This routine search the Gaia archive and downloads the stars using parallel workers.
   """

   from multiprocessing import Pool, cpu_count
   from astroquery.gaia import Gaia

   mag_nodes = get_mag_bins(min_gmag, max_gmag, area)
   n_total = len(mag_nodes)
   
   if (n_total > 1) and use_parallel:

      print("Executing %s jobs."%(n_total-1))

      nproc = int(np.min((n_total, 20, cpu_count()*2)))

      pool = Pool(nproc-1)

      args = []
      for n, node in enumerate(range(n_total-1)):
         args.append((Gaia, query, mag_nodes[n+1], mag_nodes[n], save_individual_queries, load_existing, name, n, n_total))

      tables_gaia_queries = pool.map(gaia_multi_query_run, args)

      tables_gaia = [results[0] for results in tables_gaia_queries]
      queries = [results[1] for results in tables_gaia_queries]

      result_gaia = pd.concat(tables_gaia)

      pool.close()

   else:
      result_gaia, queries = gaia_query(Gaia, query, min_gmag, max_gmag, save_individual_queries, load_existing, name, 1, 1)

   return result_gaia, queries


def plot_fields(Gaia_table, obs_table, HST_path, min_stars_alignment = 5, name = 'test.png'):
   """
   This routine plots the fields and select Gaia stars within them.
   """

   from matplotlib.patches import Polygon
   from matplotlib.collections import PatchCollection
   from shapely.geometry.polygon import Polygon as shap_polygon
   from shapely.geometry import Point

   def deg_to_hms(lat, even = True):
      from astropy.coordinates import Angle
      angle = Angle(lat, unit = u.deg)
      if even:
         if lat%30:
            string =''
         else:
            string = angle.to_string(unit=u.hour)
      else:
         string = angle.to_string(unit=u.hour)
      return string

   def deg_to_dms(lon):
      from astropy.coordinates import Angle
      angle = Angle(lat, unit = u.deg)
      string = angle.to_string(unit=u.degree)
      return string

   def coolwarm(filter, alpha):
      color = [rgb for rgb in plt.cm.coolwarm(255 * (float(filter) - 555) / (850-555))]
      color[-1] = alpha
      return color

   Gaia_table['parent_obsid'] = ""
   
   fig, ax = plt.subplots(1,1, figsize = (5.5, 5.5))
   patches = []
   ecs = []
   fcs = []
   previous_obsid = []
   gaia_stars_per_obs = []
   
   filter_range = [float(s.replace('F', '').replace('W', '').replace('LP', '')) for s in obs_table.filters]

   # We rearrange obs_table for legibility
   obs_table = obs_table.sort_values(by =['proposal_id', 'obsid'], ascending = False).reset_index(drop=True)

   for index_obs, (footprint_str, obsid, filter, obs_id) in obs_table.loc[:, ['s_region', 'obsid', 'filters', 'obs_id']].sort_values(by=['obsid']).iterrows():
      cli_progress_test(index_obs+1, len(obs_table))

      idx_Gaia_in_field = []
      gaia_stars_per_poly = [] 
      list_coo = footprint_str.split('POLYGON')[1::]

      for poly in list_coo:
         try:
            poly = list(map(float, poly.split()))
         except:
            poly = list(map(float, poly.split('J2000')[1::][0].split()))

         tuples_list = [(ra % 360, dec) for ra, dec in zip(poly[0::2], poly[1::2])]

         # Make sure the field is complete. With at least 4 vertices.
         if len(tuples_list) > 4:
            polygon = Polygon(tuples_list, True)
            ecs.append(coolwarm(filter.replace(r'F', '').replace(r'W', '').replace('LP', ''), 1))
            
            # Check if the set seems downloaded
            if os.path.isfile(HST_path+'mastDownload/HST/'+obs_id+'/'+obs_id+'_drz.fits'):
               fcs.append([0,1,0,0.2])
            else:
               fcs.append([1,1,1,0.2])

            footprint =  shap_polygon(tuples_list)

            star_counts = 0
            for idx, ra, dec in zip(Gaia_table.index, Gaia_table.ra, Gaia_table.dec):
               if Point(ra, dec).within(footprint):
                  idx_Gaia_in_field.append(idx)
                  star_counts += 1

            gaia_stars_per_poly.append(star_counts)
            
            if star_counts >= min_stars_alignment:
               patches.append(polygon)

               annotation_coo = [round(max(tuples_list)[0], 2)-0.028, round(max(tuples_list)[1], 2)-0.028]

               if annotation_coo in previous_obsid:
                  annotation_coo[1] += 0.01

               ax.annotate(index_obs+1, xy=(annotation_coo[0], annotation_coo[1]), xycoords='data', color = coolwarm(filter.replace(r'F', '').replace(r'W', '').replace('LP', ''), 1))

               previous_obsid.append(annotation_coo)

      gaia_stars_per_obs.append(sum(gaia_stars_per_poly))

      Gaia_table.loc[idx_Gaia_in_field, 'parent_obsid'] = Gaia_table.loc[idx_Gaia_in_field, 'parent_obsid'].astype(str) + '%s '%obsid

   print('\n')

   previous_obsid = np.array(previous_obsid)
   obs_table['gaia_stars_per_obs'] = gaia_stars_per_obs
   
   try:
      ra_lims = [max(Gaia_table.ra.max(), previous_obsid[:,0].max()), min(Gaia_table.ra.min(), previous_obsid[:,0].min())]
      dec_lims = [min(Gaia_table.dec.min(), previous_obsid[:,1].min()), max(Gaia_table.dec.max(), previous_obsid[:,1].max())]
   except:
      ra_lims = [Gaia_table.ra.max(), Gaia_table.ra.min()]
      dec_lims = [Gaia_table.dec.min(), Gaia_table.dec.max()]
 
   pe = PatchCollection(patches, alpha = 0.1, ec = 'None', fc = fcs, antialiased = True, lw = 1, zorder = 4)
   pf = PatchCollection(patches, alpha = 1, ec = ecs, fc = 'None', antialiased = True, lw = 1, zorder = 5)
   ax.add_collection(pe)
   ax.add_collection(pf)
   ax.plot(Gaia_table.ra, Gaia_table.dec, '.', color = '0.4', ms = 0.75, zorder = 1)
   ax.set_xlim(ra_lims[0], ra_lims[1])
   ax.set_ylim(dec_lims[0], dec_lims[1])
   ax.grid()

   ax.set_xlabel(r'$\alpha$ [deg]')
   ax.set_ylabel(r'$\delta$ [deg]')

   plt.savefig(name, bbox_inches='tight')

   with plt.rc_context(rc={'interactive': False}):
      plt.gcf().show()

   obs_table = obs_table.loc[obs_table.gaia_stars_per_obs >= min_stars_alignment]
   obs_table[''] = ['(%i)'%(ii+1) for ii in np.arange(len(obs_table))]
   
   return Gaia_table, obs_table


def search_mast(ra, dec, width, height, filters = ['any'], project = ['HST'], t_exptime_min = 50, t_exptime_max = 2500, date_second_epoch = 57531.0, time_baseline = 3650):
   """
   This routine search for HST observations in MAST at a given position.
   """

   from astroquery.mast import Catalogs
   from astroquery.mast import Observations

   ra1 = ra - width / 2 + 0.056*np.cos(np.deg2rad(dec))
   ra2 = ra + width / 2 - 0.056*np.cos(np.deg2rad(dec))
   dec1 = dec - height / 2 + 0.056
   dec2 = dec + height / 2 - 0.056

   t_max = date_second_epoch - time_baseline
   
   if filters == ['any']:
      filters=['F555W','F606W','F775W','F814W','F850LP']
   elif type(filters) is not list:
      filters = [filters]

   obs_table = Observations.query_criteria(dataproduct_type=['image'], obs_collection=['HST'], s_ra=[ra1, ra2], s_dec=[dec1, dec2], instrument_name=['ACS/WFC', 'WFC3/UVIS'], t_max=[0, t_max], filters = filters, project = project)

   data_products_by_obs = search_data_products_by_obs(obs_table)
   
   #Pandas is easier:
   obs_table = obs_table.to_pandas()
   data_products_by_obs = data_products_by_obs.to_pandas()

   # We are only interested in FLC and DRZ images
   data_products_by_obs = data_products_by_obs.loc[data_products_by_obs.project != 'HAP', :]
   obs_table = obs_table.merge(data_products_by_obs.loc[data_products_by_obs.productSubGroupDescription == 'FLC', :].groupby(['parent_obsid'])['parent_obsid'].count().rename_axis('obsid').rename('n_exp'), on = ['obsid'])

   obs_table['i_exptime'] = obs_table['t_exptime'] / obs_table['n_exp']

   #For convenience we add an extra column with the time baseline
   obs_time = Time(obs_table['t_max'], format='mjd')
   obs_time.format = 'iso'
   obs_time.out_subfmt = 'date'
   obs_table['obs_time'] = obs_time

   obs_table['t_baseline'] = round((date_second_epoch - obs_table['t_max']) / 365.2422, 2)
   obs_table['filters'] = obs_table['filters'].str.strip('; CLEAR2L CLEAR1L')

   data_products_by_obs = data_products_by_obs.merge(obs_table.loc[:, ['obsid', 'i_exptime', 'filters', 't_baseline', 's_ra', 's_dec']].rename(columns={'obsid':'parent_obsid'}), on = ['parent_obsid'])

   #We select by individual exp time:
   obs_table = obs_table.loc[(obs_table.i_exptime > t_exptime_min) & (obs_table.i_exptime < t_exptime_max) & (obs_table.t_baseline > time_baseline / 365.2422 )]
   data_products_by_obs = data_products_by_obs.loc[(data_products_by_obs.i_exptime > t_exptime_min) & (data_products_by_obs.i_exptime < t_exptime_max) & (data_products_by_obs.t_baseline > time_baseline / 365.2422 )]

   return obs_table.astype({'obsid': 'int64'}).reset_index(drop = False), data_products_by_obs.astype({'parent_obsid': 'int64'}).reset_index(drop = False)


def search_data_products_by_obs(obs_table):
   """
   This routine search for images in MAST related to the given observations table.
   """
   
   from astroquery.mast import Observations

   data_products_by_obs = Observations.get_product_list(obs_table)

   return data_products_by_obs[((data_products_by_obs['productSubGroupDescription'] == 'FLC') | (data_products_by_obs['productSubGroupDescription'] == 'DRZ')) & (data_products_by_obs['obs_collection'] == 'HST')]


def download_HST_images(data_products_by_obs, path = './'):
   """
   This routine downloads the selected HST images from MAST.
   """
   
   from astroquery.mast import Observations

   images = Observations.download_products(data_products_by_obs, download_dir=path)
   
   print('')

   return images


def create_dir(path):
   """
   This routine creates directories.
   """

   if not os.path.isdir(path):
      try:
         os.mkdir(path)
      except OSError:  
         print ("Creation of the directory %s failed" % path)
      else:  
         print ("Successfully created the directory %s " % path)


def members_prob(table, clf, vars, clipping_prob = 3, data_0 = None):
   """
   This routine will find probable members through scoring of a passed model (clf).
   """

   has_vars = table.loc[:, vars].notnull().all(axis = 1)

   data = table.loc[has_vars, vars]

   clustering_data = table.loc[has_vars, 'clustering_data'] == 1

   results = pd.DataFrame(columns = ['member_clustering_prob', 'member_clustering'], index = table.index)

   if (clustering_data.sum() > 1):

      if data_0 is None:
         data_0 = data.loc[clustering_data, vars].median().values

      data -= data_0

      data_std = data.loc[clustering_data, :].std().values

      clf.fit(data.loc[clustering_data, :] / data_std)

      log_prob = clf.score_samples(data / data_std)
      label_GMM = log_prob >= np.median(log_prob[clustering_data])-clipping_prob*np.std(log_prob[clustering_data])

      results.loc[has_vars, 'member_clustering_prob'] = log_prob
      results.loc[has_vars, 'member_clustering'] = label_GMM

   return results


def pm_cleaning_GMM_recursive(table, vars, alt_table = None, data_0 = None, n_components = 1, covariance_type = 'full', clipping_prob = 3, plots = True, verbose = False, plot_name = ''):
   """
   This routine iteratively find members using a Gaussian mixture model.
   """
   
   table['real_data'] = True
   
   try:
      table['clustering_data']
   except:
      table['clustering_data'] = 1

   if alt_table is not None:
      alt_table['real_data'] = False
      alt_table['clustering_data'] = 0
      table = pd.concat([table, alt_table], ignore_index = True, sort=True)

   clf = mixture.GaussianMixture(n_components = n_components, covariance_type = covariance_type, means_init = np.zeros((n_components, len(vars))))

   convergence = False
   iteration = 0
   while not convergence:
      if verbose:
         print("\rIteration %i, %i objects remain."%(iteration, table.clustering_data.sum()))

      clust = table.loc[:, vars+['clustering_data']]

      if iteration > 3:
         data_0 = None

      fitting = members_prob(clust, clf, vars, clipping_prob = clipping_prob,  data_0 = data_0)
      
      table['member_clustering'] = fitting.member_clustering
      table['member_clustering_prob'] = fitting.member_clustering_prob
      table['clustering_data'] = (table.clustering_data == 1) & (fitting.member_clustering == 1)  & (table.real_data == 1)
      
      if (iteration > 999):
         convergence = True
      elif iteration > 0:
         convergence = fitting.equals(previous_fitting)

      previous_fitting = fitting.copy()
      iteration += 1

   if plots == True:
      plt.close('all')
      fig, ax1 = plt.subplots(1, 1)
      ax1.plot(table.loc[table.real_data == 1 ,vars[0]], table.loc[table.real_data == 1 , vars[1]], 'k.', ms = 0.5, zorder = 0)
      ax1.scatter(table.loc[table.clustering_data == 1 ,vars[0]].values, table.loc[table.clustering_data == 1 ,vars[1]].values, c = table.loc[table.clustering_data == 1, 'member_clustering_prob'].values, s = 1, zorder = 1)
      ax1.set_xlabel(r'$\mu_{\alpha*}$')
      ax1.set_ylabel(r'$\mu_{\delta}$')
      ax1.set_xlim(table.loc[table.real_data == 1 , vars[0]].mean()-5*table.loc[table.real_data == 1 , vars[0]].std(), table.loc[table.real_data == 1 , vars[0]].mean()+5*table.loc[table.real_data == 1 , vars[0]].std())
      ax1.set_ylim(table.loc[table.real_data == 1 , vars[1]].mean()-5*table.loc[table.real_data == 1 , vars[1]].std(), table.loc[table.real_data == 1 , vars[1]].mean()+5*table.loc[table.real_data == 1 , vars[1]].std())      
      ax1.grid()
      plt.savefig(plot_name, bbox_inches='tight')
      plt.close('all')

   if alt_table is not None:
      return fitting.loc[table.real_data == 1, 'member_clustering'], fitting.loc[table.real_data == 0, 'member_clustering']
   else:
      return fitting.member_clustering


def remove_file(file_name):
   """
   This routine removes files
   """

   try:
      os.remove(file_name)
   except:
      pass


def applied_pert(XYmqxyrd_filename):
   """
   This routine will read the XYmqxyrd file and find whether the PSF perturbation worked . If file not found then False.
   """

   try:
      f = open(XYmqxyrd_filename, 'r')
      perts = []
      for index, line in enumerate(f):
         if '# CENTRAL PERT PSF' in line:
            for index, line in enumerate(f):
               if len(line[10:-1].split()) > 0:
                  perts.append([float(pert) for pert in line[10:-1].split()])
               else:
                  break
            f.close()
            break
      if len(perts) > 0:
         return np.array(perts).ptp() != 0
      else:
         print('CAUTION: No information about PSF perturbation found in %s!'%XYmqxyrd_filename)
         return True
   except:
      return False


def get_fmin(i_exptime):
   """
   This routine returns the best value for FMIN to be used in hst1pass execution based on the integration time
   """

   # These are the values for fmin at the given lower_exptime and upper_exptime
   lower_fmin, upper_fmin = 1000, 5000
   lower_exptime, upper_exptime = 50, 500

   return min(max((int(lower_fmin + (upper_fmin - lower_fmin) * (i_exptime - lower_exptime) / (upper_exptime - lower_exptime)), 1000)), 10000)


def hst1pass_multiproc_run(args):
   """
   This routine pipes hst1pass into multiple threads.
   """

   return hst1pass(*args)


def hst1pass(HST_path, obs_id, HST_image, force_fmin, force_hst1pass, remove_previous_XYmqxyrd, verbose):
   """
   This routine will execute hst1pass Fortran routine using the correct arguments.
   """

   #Define HST servicing missions times
   t_sm3 = Time('2002-03-12').mjd
   t_sm4 = Time('2009-05-24').mjd

   output_dir = HST_path+'mastDownload/HST/'+obs_id+'/'
   HST_image_filename = output_dir+HST_image
   XYmqxyrd_filename = HST_image_filename.split('.fits')[0]+'.XYmqxyrd'

   if not os.path.isfile(XYmqxyrd_filename) or force_hst1pass:
      if verbose:
         print('Finding sources in', HST_image)
      
      #Read information from the header of the image
      hdul = fits.open(HST_image_filename)
      instrument = hdul[0].header['INSTRUME']
      detector = hdul[0].header['DETECTOR']
      if detector == 'UVIS':
         detector = 'UV'

      try:
         filter = [filter for filter in [hdul[0].header['FILTER1'], hdul[0].header['FILTER2']] if 'CLEAR' not in filter][0]
      except:
         filter = hdul[0].header['FILTER']
      t_max = hdul[0].header['EXPEND']
      sm = ''
      if instrument == 'ACS':
         if (t_max > t_sm3) & (t_max < t_sm4):
            sm = '_SM3'
         elif (t_max > t_sm4):
            sm = '_SM4'

      if force_fmin is None:
         fmin = get_fmin(hdul[0].header['EXPTIME'])
      else:
         fmin = force_fmin
      
      if verbose:
         print('%s exptime = %.1f. Using fmin = %i'%(HST_image, hdul[0].header['EXPTIME'], fmin))

      psf_filename = './Auxiliary/STDPSFs/%s%s/PSFSTD_%s%s_%s%s.fits'%(instrument, detector, instrument, detector, filter, sm)
      gdc_filename = './Auxiliary/STDGDCs/%s%s/STDGDC_%s%s_%s.fits'%(instrument, detector, instrument, detector, filter)

      if remove_previous_XYmqxyrd:
         remove_file(XYmqxyrd_filename)

      pert_grid = 5
      while not applied_pert(XYmqxyrd_filename) and (pert_grid > 0):
         bashCommand = "./Auxiliary/hst1pass.e HMIN=5 FMIN=%s PMAX=999999 GDC=%s PSF=%s PERT%i=AUTO OUT=XYmqxyrd OUTDIR=%s %s"%(fmin, gdc_filename, psf_filename, pert_grid, output_dir, HST_image_filename)

         process = subprocess.Popen(bashCommand.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

         output, error = process.communicate()
         pert_grid -= 1

      if verbose:
         print('%s PERT PSF = %sx%s'%(HST_image, pert_grid, pert_grid))

      remove_file(HST_image.replace('_flc','_psf'))


def launch_hst1pass(flc_images, HST_obs_to_use, HST_path, force_fmin = None, force_hst1pass = True, remove_previous_XYmqxyrd = True, verbose = True, use_parallel = True):
   """
   This routine will launch hst1pass routine in parallel or serial 
   """
   from multiprocessing import Pool, cpu_count
   
   print("\n---------------------------------")
   print("Finding sources in the HST images")
   print("---------------------------------")

   args = []
   for HST_image_obsid in [HST_obs_to_use] if not isinstance(HST_obs_to_use, list) else HST_obs_to_use:
      for index_image, (obs_id, HST_image) in flc_images.loc[flc_images['parent_obsid'] == HST_image_obsid, ['obs_id', 'productFilename']].iterrows():
         args.append((HST_path, obs_id, HST_image, force_fmin, force_hst1pass, remove_previous_XYmqxyrd, verbose))
   
   if (len(args) > 1) and use_parallel:
      pool = Pool(min(cpu_count(), len(args)))
      pool.map(hst1pass_multiproc_run, args)
      pool.close()
   
   else:
      for arg in args:
         hst1pass_multiproc_run(arg)

   remove_file('LOG.psfperts.fits')
   remove_file('fort.99')


def check_mat(mat_filename, iteration, min_stars_alignment = 100, alpha = 0.01, center_tolerance = 1e-3, plots = True, fix_mat = True, clipping_prob = 2, verbose= True):
   """
   This routine will read the transformation file MAT and provide a quality flag based on how Gaussian the transformation is.
   """

   mat = np.loadtxt(mat_filename)

   if fix_mat:
      clf = mixture.GaussianMixture(n_components = 1, covariance_type = 'spherical')
      clf.fit(mat[:, 6:8])
      log_prob = clf.score_samples(mat[:, 6:8])
      good_for_alignment = log_prob >= np.median(log_prob)-clipping_prob*np.std(log_prob)
      if verbose and (good_for_alignment.sum() < len(log_prob)): print('   Fixing MAT file for next iteration.')
      np.savetxt(mat_filename, mat[good_for_alignment, :], fmt='%12.4f')
   else:
      good_for_alignment = [True]*len(mat)

   with warnings.catch_warnings():
      warnings.simplefilter("ignore")
      # Obtain the Shapiro–Wilk statistics
      stat, p = stats.shapiro(mat[:, 6:8])
   
   valid = [p > alpha,  (mat[:, 6:8].mean(axis = 0) < center_tolerance).all(), len(mat) > min_stars_alignment]

   if plots:
      plt.close()
      fig, ax1 = plt.subplots(1, 1)
      try:
         ax1.plot(mat[~good_for_alignment,6], mat[~good_for_alignment,7], '.', ms = 2, label = 'Rejected')
      except:
         pass
      ax1.plot(mat[good_for_alignment,6], mat[good_for_alignment,7], '.', ms = 2, label = 'Used')
      ax1.axvline(x=0, linewidth = 0.75, color = 'k')
      ax1.axhline(y=0, linewidth = 0.75, color = 'k')
      ax1.set_xlabel('X_Gaia - X_HST [pixels]')
      ax1.set_ylabel('Y_Gaia - Y_HST [pixels]')
      ax1.grid()
      
      add_inner_title(ax1, 'Valid=%s\np=%.4f\ncen=(%.4f,%.4f)\nnum=%i'%(all(valid), p, mat[:, 6].mean(), mat[:, 7].mean(), len(mat)), 1)
      
      plt.savefig(mat_filename.split('.MAT')[0]+'_MAT_%i.png'%iteration, bbox_inches='tight')
      plt.close()

   return valid


def xym2pm_Gaia(iteration, Gaia_HST_table_field, Gaia_HST_table_filename, HST_image_filename, lnk_filename, mat_filename, amp_filename, date_reference_second_epoch, only_use_members, rewind_stars, force_pixel_scale, force_max_separation, force_use_sat, fix_mat, force_wcs_search_radius, min_stars_alignment, verbose, force_xym2pm, plots, force_use_amp, min_stars_amp, use_mean):
   """
   This routine will execute xym2pm_Gaia Fortran routine using the correct arguments.
   """

   hdul = fits.open(HST_image_filename)
   t_max = hdul[0].header['EXPEND']
   ra_cent = hdul[0].header['RA_TARG']
   dec_cent = hdul[0].header['DEC_TARG']
   try:
      filter = [filter for filter in [hdul[0].header['FILTER1'], hdul[0].header['FILTER2']] if 'CLEAR' not in filter][0]
   except:
      filter = hdul[0].header['FILTER']

   t_baseline = (date_reference_second_epoch.mjd - t_max) / 365.25
   timeref = str(date_reference_second_epoch.jyear)

   if force_pixel_scale is None:
      # Infer pixel scale from the header
      pixel_scale = round(np.mean([proj_plane_pixel_scales(WCS(hdul[3].header)).mean(), proj_plane_pixel_scales(WCS(hdul[5].header)).mean()])*3600, 3)
   else:
      pixel_scale = force_pixel_scale

   pixel_scale_mas = 1e3 * pixel_scale

   if (iteration > 0) & (rewind_stars):
      align_var = ['ra', 'ra_error', 'dec', 'dec_error', 'hst_gaia_pmra_%s'%use_mean, 'hst_gaia_pmra_%s_error'%use_mean, 'hst_gaia_pmdec_%s'%use_mean, 'hst_gaia_pmdec_%s_error'%use_mean, 'gmag', 'use_for_alignment']
   else:
      align_var = ['ra', 'ra_error', 'dec', 'dec_error', 'pmra', 'pmra_error', 'pmdec', 'pmdec_error', 'gmag', 'use_for_alignment']

   if not os.path.isfile(lnk_filename) or force_xym2pm:

      f = open(Gaia_HST_table_filename, 'w+')
      f.write('# ')
      Gaia_HST_table_field.loc[:, align_var].astype({'use_for_alignment': 'int32'}).to_csv(f, index = False, sep = ' ', na_rep = 0)
      f.close()

      # Here it goes the executable line. Input values can be fine-tuned here
      if (iteration > 0) & (rewind_stars):
         time = ' TIME=%s'%str(round(Time(t_max, format='mjd').jyear, 3))
      else:
         time = ''

      if force_use_sat:
         use_sat = ' USESAT+'
      else:
         use_sat = ''

      if (fix_mat) and (iteration > 0) and not rewind_stars:
         use_mat = " MAT=\"%s\" USEMAT+"%(mat_filename.split('./')[1])
      else:
         use_mat = ''

      if force_wcs_search_radius is None:
         use_brute = ''
      else:
         use_brute = ' BRUTE=%.1f'%force_wcs_search_radius

      if force_max_separation is None:
         max_separation = 5.0
      else:
         max_separation = force_max_separation

      if force_use_amp:
         use_amp = ' NAMP=%i AMP+'%min_stars_amp
      else:
         use_amp = ''

      bashCommand = "./Auxiliary/xym2pm_Gaia.e %s %s RACEN=%f DECEN=%f XCEN=5000.0 YCEN=5000.0 PSCL=%s SIZE=10000 DISP=%.1f NMIN=%i TIMEREF=%s%s%s%s%s%s"%(Gaia_HST_table_filename, HST_image_filename, ra_cent, dec_cent, pixel_scale, max_separation, min_stars_alignment, timeref, time, use_sat, use_mat, use_brute, use_amp)

      process = subprocess.Popen(bashCommand.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      output, error = process.communicate()

      f = open(lnk_filename.replace(".LNK", "_6p_transformation.txt"), 'w+')
      f.write(output.decode("utf-8"))
      f.close()

   try:
      #Next are threshold rejection values for the MAT files.
      if (iteration > 0) and only_use_members and not rewind_stars:
         alpha = 1e-6
      else:
         alpha = 1e-64

      valid_mat = check_mat(mat_filename, iteration, min_stars_alignment, alpha = alpha, center_tolerance = 1e-2, fix_mat = fix_mat, clipping_prob = 3., plots = plots, verbose = verbose)

      if all(valid_mat):

         f = open(lnk_filename, 'r')
         header = [w.replace('m_hst', filter) for w in f.readline().rstrip().strip("# ").split(' ')]
         lnk = pd.read_csv(f, names=header, sep = '\s+', comment='#', na_values = 0.0).set_index(Gaia_HST_table_field.index)
         f.close()

         if os.path.isfile(amp_filename):
            f = open(amp_filename, 'r')
            header = f.readline().rstrip().strip("# ").split(' ')
            ampfile = pd.read_csv(f, names=header, sep = '\s+', comment='#', na_values = 0.0).set_index(Gaia_HST_table_field.index)
            f.close()
            lnk['xhst_gaia'] = ampfile.xhst_amp_gaia.values
            lnk['yhst_gaia'] = ampfile.yhst_amp_gaia.values

            f = open(lnk_filename.replace(".LNK", "_amp.LNK"), 'w+')
            f.write('# ')
            lnk.to_csv(f, index = False, sep = ' ', na_rep = 0)
            f.close()

         # Positional and mag error is know to be proportional to the QFIT parameter.
         eradec_hst = lnk.q_hst
         # Assign the maximum (worst) QFIT parameter to saturated stars.
         eradec_hst[lnk.xhst_gaia.notnull()] = lnk[lnk.xhst_gaia.notnull()].q_hst.replace({np.nan:lnk.q_hst.max()})
         # 0.85 seems reasonable, although this may be tuned through an empirical function.
         eradec_hst *= pixel_scale_mas * 0.85

         lnk['relative_hst_gaia_pmra'] = -(lnk.x_gaia - lnk.xhst_gaia) * pixel_scale_mas / t_baseline
         lnk['relative_hst_gaia_pmdec'] = (lnk.y_gaia - lnk.yhst_gaia) * pixel_scale_mas / t_baseline

         # Notice the 1e3. xym2pm_Gaia.e takes the error in mas but returns it in arcsec.
         lnk['relative_hst_gaia_pmra_error'] = eradec_hst / t_baseline
         lnk['relative_hst_gaia_pmdec_error'] = eradec_hst / t_baseline
         lnk['gaia_dra_uncertaintity'] = 1e3 * lnk.era_gaia / t_baseline
         lnk['gaia_ddec_uncertaintity'] = 1e3 * lnk.edec_gaia / t_baseline
         # Here we arbitrarily divided by 100 just for aesthetic reasons.
         lnk['%s_error'%filter] = lnk.q_hst.replace({0:lnk.q_hst.max()}) * 0.01

         match = lnk.loc[:, [filter, '%s_error'%filter, 'relative_hst_gaia_pmra', 'relative_hst_gaia_pmra_error', 'relative_hst_gaia_pmdec', 'relative_hst_gaia_pmdec_error', 'gaia_dra_uncertaintity', 'gaia_ddec_uncertaintity']]

         print('-->%s: matched %i stars.'%(os.path.basename(HST_image_filename), len(match)))

      else:
         if verbose:
            print('-->%s: bad quality match:'%os.path.basename(HST_image_filename))
            if valid_mat[0] != True:
               print('   Non Gaussian distribution found in the transformation')
            if valid_mat[1] != True:
               print('   Not (0,0) average of the distribution')
            if valid_mat[2] != True:
               print('   Less than %i stars used during the transformation'%min_stars_alignment)
            print('   Skipping image.')
         match = pd.DataFrame()
   except:
      print('-->%s: no match found.'%os.path.basename(HST_image_filename))
      match = pd.DataFrame()

   return match


def xym2pm_Gaia_multiproc(args):
   """
   This routine pipes xym2pm_Gaia into multiple threads.
   """

   return xym2pm_Gaia(*args)


def launch_xym2pm_Gaia(Gaia_HST_table, data_products_by_obs, HST_obs_to_use, HST_path, date_reference_second_epoch, only_use_members = False, rewind_stars = True, force_pixel_scale = None, force_max_separation = None, force_use_sat = True, fix_mat = True, force_use_amp = False, min_stars_amp = 25, force_wcs_search_radius = None, n_components = 1, clipping_prob = 6, min_stars_alignment = 100, use_mean = 'wmean', plots = True, verbose = True, force_xym2pm = True, remove_previous_files = True, use_parallel = True, plot_name = ''):
   """
   This routine will launch xym2pm_Gaia Fortran routine in parallel or serial using the correct arguments.
   """
   from multiprocessing import Pool, cpu_count
   import re
   import itertools

   n_images = len(data_products_by_obs.loc[data_products_by_obs['parent_obsid'].isin([HST_obs_to_use] if not isinstance(HST_obs_to_use, list) else HST_obs_to_use), :])
   
   if (n_images > 1) and use_parallel:
      pool = Pool(min(cpu_count(), n_images))
      plots = False
      verbose = False

   Gaia_HST_table['use_for_alignment'] = True
   convergence = False
   iteration = 0

   pmra_evo = []
   pmdec_evo = []

   pmra_diff_evo = []
   pmdec_diff_evo = []

   hst_gaia_pmra_lsqt_evo = []
   hst_gaia_pmdec_lsqt_evo = []

   while not convergence:
      print("\n-----------")
      print("Iteration %i"%(iteration))
      print("-----------")

      args = []
      for index_image, (obs_id, HST_image) in data_products_by_obs.loc[data_products_by_obs['parent_obsid'].isin([HST_obs_to_use] if not isinstance(HST_obs_to_use, list) else HST_obs_to_use), ['obs_id', 'productFilename']].iterrows():

         HST_image_filename = HST_path+'mastDownload/HST/'+obs_id+'/'+HST_image
         Gaia_HST_table_filename = HST_path+'Gaia_%s.ascii'%HST_image.split('.fits')[0]
         lnk_filename = HST_image_filename.split('.fits')[0]+'.LNK'
         mat_filename = HST_image_filename.split('.fits')[0]+'.MAT'
         amp_filename = HST_image_filename.split('.fits')[0]+'.AMP'

         if iteration == 0:
            if remove_previous_files:
               remove_file(mat_filename)
               remove_file(lnk_filename)
               remove_file(amp_filename)

            Gaia_HST_table = find_stars_to_align(Gaia_HST_table, HST_image_filename)

            n_field_stars = Gaia_HST_table.loc[Gaia_HST_table['HST_image'].str.contains(str(obs_id)), 'use_for_alignment'].count()
            u_field_stars = Gaia_HST_table.loc[Gaia_HST_table['HST_image'].str.contains(str(obs_id)), 'use_for_alignment'].sum()

            if (n_field_stars < min_stars_amp*4):
               print('Not enough stars in %s as to separate amplifiers. Only one channel will be used.'%HST_image)
               force_use_amp = False
            if (u_field_stars < min_stars_alignment):
               Gaia_HST_table.loc[Gaia_HST_table['HST_image'].str.contains(str(obs_id)), 'use_for_alignment'] = True

         Gaia_HST_table_field = Gaia_HST_table.loc[Gaia_HST_table['HST_image'].str.contains(str(obs_id)), :]

         args.append((iteration, Gaia_HST_table_field, Gaia_HST_table_filename, HST_image_filename, lnk_filename, mat_filename, amp_filename, date_reference_second_epoch, only_use_members, rewind_stars, force_pixel_scale, force_max_separation, force_use_sat, fix_mat, force_wcs_search_radius, min_stars_alignment, verbose, force_xym2pm, plots, force_use_amp, min_stars_amp, use_mean))

      if (len(args) > 1) and use_parallel:
         lnks = pool.map(xym2pm_Gaia_multiproc, args)

      else:
         lnks = []
         for arg in args:
            lnks.append(xym2pm_Gaia_multiproc(arg))

      try:
         lnks = pd.concat(lnks, sort=True)
      except:
         print('WARNING: No match could be found for any of the images. Please try with other parameters.\nExiting now.')
         sys.exit(1)

      lnks_averaged = lnks.groupby(lnks.index).apply(weighted_avg_err)

      # Gaia positional errors have to be added in quadrature
      lnks_averaged['relative_hst_gaia_pmra_mean_error'] = np.sqrt(lnks_averaged['relative_hst_gaia_pmra_mean_error']**2 + lnks_averaged['gaia_dra_uncertaintity_mean']**2)
      lnks_averaged['relative_hst_gaia_pmdec_mean_error'] = np.sqrt(lnks_averaged['relative_hst_gaia_pmdec_mean_error']**2 + lnks_averaged['gaia_ddec_uncertaintity_mean']**2)
      lnks_averaged['relative_hst_gaia_pmra_wmean_error'] = np.sqrt(lnks_averaged['relative_hst_gaia_pmra_wmean_error']**2 + lnks_averaged['gaia_dra_uncertaintity_mean']**2)
      lnks_averaged['relative_hst_gaia_pmdec_wmean_error'] = np.sqrt(lnks_averaged['relative_hst_gaia_pmdec_wmean_error']**2 + lnks_averaged['gaia_ddec_uncertaintity_mean']**2)

      # Remove redundant columns
      lnks_averaged = lnks_averaged.drop(columns=[col for col in lnks_averaged if col.startswith('gaia')])

      try:
         Gaia_HST_table.drop(columns = lnks_averaged.columns, inplace = True)
      except:
         pass

      # Obtain absolute PMs
      Gaia_HST_table = absolute_pm(Gaia_HST_table.join(lnks_averaged))

      # Membership selection
      if only_use_members:
         if iteration == 0:
            # Select stars in theCMD
            hst_filters = [col for col in lnks_averaged.columns if ('F' in col) & ('error' not in col) & ('std' not in col) & ('_mean' not in col)]
            hst_filters.sort()

            min_HST_HST_stars = 0.9*len(Gaia_HST_table.loc[:, ['relative_hst_gaia_pmra_%s'%use_mean, 'relative_hst_gaia_pmdec_%s'%use_mean]].dropna())

            if (len(hst_filters) >= 2):
               for f1, f2 in itertools.combinations(hst_filters, 2):
                  if (len((Gaia_HST_table[f1] - Gaia_HST_table[f2]).dropna()) >=  min_HST_HST_stars):
                     Gaia_HST_table['clustering_data'] =  manual_select_from_cmd((Gaia_HST_table[hst_filters[0]]-Gaia_HST_table[hst_filters[1]]).rename('%s - %s'%(hst_filters[0], hst_filters[1])), Gaia_HST_table[hst_filters[1]])
                     break
                  else:
                     Gaia_HST_table['clustering_data'] = True
            else:
               for cmd_filter in hst_filters:
                  if int(re.findall(r'\d+', cmd_filter)[0]) <= 606:
                     HST_Gaia_color = (Gaia_HST_table[cmd_filter]-Gaia_HST_table['gmag']).rename('%s - Gmag'%cmd_filter)
                  else:
                     HST_Gaia_color = (Gaia_HST_table['gmag']-Gaia_HST_table[cmd_filter]).rename('Gmag - %s'%cmd_filter)

                  Gaia_HST_table['%s_clustering_data_cmd'%cmd_filter] =  manual_select_from_cmd(HST_Gaia_color, Gaia_HST_table['gmag'].rename('Gmag'))

               cmd_clustering_filters = [col for col in Gaia_HST_table.columns if '_clustering_data_cmd' in col]
               Gaia_HST_table['clustering_data'] = (Gaia_HST_table.loc[:, cmd_clustering_filters] == True).any(axis = 1)
               Gaia_HST_table.drop(columns = cmd_clustering_filters, inplace = True)
         
         # Select stars in the PM space asuming spherical covariance (Reasonable for dSphs and globular clusters) 
         pm_clustering = pm_cleaning_GMM_recursive(Gaia_HST_table.copy(), ['relative_hst_gaia_pmra_%s'%use_mean, 'relative_hst_gaia_pmdec_%s'%use_mean], data_0 = [0, 0], n_components = n_components, covariance_type = 'spherical', clipping_prob = clipping_prob, plots = plots, plot_name = '%s_PM_sel_%i'%(plot_name, iteration))
         new_use_for_alignment = pm_clustering & Gaia_HST_table.clustering_data

         Gaia_HST_table['use_for_alignment'] = new_use_for_alignment

      elif not rewind_stars:
         convergence = True
      elif iteration > 19:
         convergence = True
      
      # Useful statistics:
      id_pms = np.isfinite(Gaia_HST_table['hst_gaia_pmra_%s'%use_mean]) & np.isfinite(Gaia_HST_table['hst_gaia_pmdec_%s'%use_mean])
      
      pmra_evo.append(Gaia_HST_table.loc[id_pms, 'hst_gaia_pmra_%s'%use_mean])
      pmdec_evo.append(Gaia_HST_table.loc[id_pms, 'hst_gaia_pmdec_%s'%use_mean])

      hst_gaia_pmra_lsqt_evo.append(np.nanstd( (Gaia_HST_table.loc[id_pms, 'hst_gaia_pmra_%s'%use_mean]  - Gaia_HST_table.loc[id_pms, 'pmra'])))
      hst_gaia_pmdec_lsqt_evo.append(np.nanstd( (Gaia_HST_table.loc[id_pms, 'hst_gaia_pmdec_%s'%use_mean] - Gaia_HST_table.loc[id_pms, 'pmdec'])))
      print("-----------")
      print('RMS(PM_HST+Gaia - PM_Gaia) = (%.4e, %.4e) m.a.s.' %(hst_gaia_pmra_lsqt_evo[-1], hst_gaia_pmdec_lsqt_evo[-1]))

      if iteration > 19:
         print('\nWARNING: Max number of iterations reached: Something might have gone wrong!\nPlease check the results carefully.')
         convergence = True

      elif iteration > 0:
         pmra_diff_evo.append(np.nanmean(pmra_evo[-1] - pmra_evo[-2]))
         pmdec_diff_evo.append(np.nanmean(pmdec_evo[-1] - pmdec_evo[-2]))
         
         # If rewind_stars is True, the code will converge when the difference between interations is smaller than the error in PMs
         threshold = np.nanmean(Gaia_HST_table.loc[id_pms, ['relative_hst_gaia_pmra_%s_error'%use_mean, 'relative_hst_gaia_pmdec_%s_error'%use_mean]].mean())*1e-1

         print('PM variation = (%.4e, %.4e)  m.a.s.' %(pmra_diff_evo[-1], pmdec_diff_evo[-1]))
         print('Threshold = %.4e  m.a.s.'%threshold)

         convergence = (np.abs(pmra_diff_evo[-1]) <= threshold) & (np.abs(pmdec_diff_evo[-1]) <= threshold)

      iteration += 1

   if (n_images > 1) and use_parallel:
      pool.close()

   Gaia_HST_table = Gaia_HST_table[Gaia_HST_table['relative_hst_gaia_pmdec_%s'%use_mean].notnull() & Gaia_HST_table['relative_hst_gaia_pmra_%s'%use_mean].notnull()]

   pmra_evo = np.array(pmra_evo).T
   pmdec_evo = np.array(pmdec_evo).T

   hst_gaia_pm_lsqt_evo = np.array([hst_gaia_pmra_lsqt_evo, hst_gaia_pmdec_lsqt_evo]).T
   pm_diff_evo = np.array([pmra_diff_evo, pmdec_diff_evo]).T

   if iteration > 1:
      try:
         plt.close()
         fig, (ax1, ax2) = plt.subplots(2, 1, sharex = True)

         ax1.plot(np.arange(iteration-2)+1, np.abs(pm_diff_evo[:,0]), '-', label = r'$\Delta(\mu_{\alpha *})$')
         ax1.plot(np.arange(iteration-2)+1, np.abs(pm_diff_evo[:,1]), '-', label = r'$\Delta(\mu_{\delta})$')

         ax2.plot(np.arange(iteration), hst_gaia_pm_lsqt_evo[:,0], '-', label = r'RMS$(\mu_{\alpha *, HST+Gaia} - \mu_{\alpha *, Gaia})$')
         ax2.plot(np.arange(iteration), hst_gaia_pm_lsqt_evo[:,1], '-', label = r'RMS$(\mu_{\delta, HST+Gaia} - \mu_{\delta, Gaia})$')

         ax1.axhline(y=threshold, linewidth = 0.75, color = 'k')
         ax1.set_ylabel(r'$\Delta(\mu)$')
         ax2.set_ylabel(r'RMS$(\mu_{HST-Gaia} - \mu_{Gaia})$')
         ax2.set_xlabel(r'iteration #')

         ax1.grid()
         ax2.grid()

         ax1.legend(shadow=True, fancybox=True)
         ax2.legend(shadow=True, fancybox=True)

         plt.savefig('%s_PM_RMS_iterations.pdf'%plot_name, bbox_inches='tight')
         plt.close()
      except:
         pass

   return Gaia_HST_table


def find_stars_to_align(stars_catalog, HST_image_filename):
   """
   This routine will find which stars from stars_catalog within and HST image.
   """

   from shapely.geometry.polygon import Polygon as shap_polygon
   from shapely.geometry import Point
   from shapely.ops import unary_union

   HST_image = HST_image_filename.split('/')[-1].split('.fits')[0]
   
   hdu = fits.open(HST_image_filename)
   
   if 'HST_image' not in stars_catalog.columns:
      stars_catalog['HST_image'] = ""

   idx_Gaia_in_field = []
   footprint = []
   for ii in [2, 5]:
      wcs = WCS(hdu[ii].header)
      footprint_chip = wcs.calc_footprint()

      #We add 10 arcsec of HST pointing error to the footprint to ensure we have all the stars.
      center_chip = np.mean(footprint_chip, axis = 0)

      footprint_chip[np.where(footprint_chip[:,0] < center_chip[0]),0] -= 0.0028*np.cos(np.deg2rad(center_chip[1]))
      footprint_chip[np.where(footprint_chip[:,0] > center_chip[0]),0] += 0.0028*np.cos(np.deg2rad(center_chip[1]))

      footprint_chip[np.where(footprint_chip[:,1] < center_chip[1]),1] -= 0.0028
      footprint_chip[np.where(footprint_chip[:,1] > center_chip[1]),1] += 0.0028

      tuples_coo = [(ra % 360, dec) for ra, dec in zip(footprint_chip[:, 0], footprint_chip[:, 1])]
      footprint.append(shap_polygon(tuples_coo))
   
   footprint = unary_union(footprint)

   for idx, ra, dec in zip(stars_catalog.index, stars_catalog.ra, stars_catalog.dec):
      if Point(ra, dec).within(footprint):
         idx_Gaia_in_field.append(idx)

   stars_catalog.loc[idx_Gaia_in_field, 'HST_image'] = stars_catalog.loc[idx_Gaia_in_field, 'HST_image'].astype(str) + '%s '%HST_image

   return stars_catalog


def get_errors(data, used_cols = None):
   """
   Obtain corresponding errors for the cols. If the error is not available it will assume the std of the entire distribution.
   """

   errors = pd.DataFrame(index = data.index)

   cols = data.columns
   if not used_cols:
      used_cols = [x for x in cols if not '_error' in x]

   for col in used_cols:
      if '%s_error'%col in cols:
         errors['%s_error'%col] = data['%s_error'%col]
      else:
         errors['%s_error'%col] = np.std(data[col])

   return errors


def weighted_avg_err(table):
   """
   Weighted average its error and the standard deviation.
   """
   var_cols = [x for x in table.columns if not '_error' in x]
   
   x_i = table.loc[:, var_cols]
   ex_i = get_errors(table, used_cols = var_cols)
   ex_i.columns = ex_i.columns.str.rstrip('_error')

   weighted_variance = (1./(1./ex_i**2).sum(axis = 0))
   weighted_avg = ((x_i.div(ex_i.values**2)).sum(axis = 0) * weighted_variance.values).add_suffix('_wmean')
   weighted_avg_error = np.sqrt(weighted_variance[~weighted_variance.index.duplicated()]).add_suffix('_wmean_error')

   avg = x_i.mean().add_suffix('_mean')
   avg_error = x_i.std().add_suffix('_mean_error')/np.sqrt(len(x_i))
   std = x_i.std().add_suffix('_std')

   return pd.concat([weighted_avg, weighted_avg_error, avg, avg_error, std])


def absolute_pm(table):
   """
   This routine computes the absolute PM just adding the absolute differences between Gaia and HST PMs.
   """   

   pm_differences_wmean = table.loc[:, ['pmra', 'pmdec']] - table.loc[:, ['relative_hst_gaia_pmra_wmean', 'relative_hst_gaia_pmdec_wmean']].values
   pm_differences_wmean_error = np.sqrt(table.loc[:, ['pmra_error', 'pmdec_error']]**2 + table.loc[:, ['relative_hst_gaia_pmra_wmean_error', 'relative_hst_gaia_pmdec_wmean_error']].values**2)

   pm_differences_mean = table.loc[:, ['pmra', 'pmdec']] - table.loc[:, ['relative_hst_gaia_pmra_mean', 'relative_hst_gaia_pmdec_mean']].values
   pm_differences_mean_error = np.sqrt(table.loc[:, ['pmra_error', 'pmdec_error']]**2 + table.loc[:, ['relative_hst_gaia_pmra_mean_error', 'relative_hst_gaia_pmdec_mean_error']].values**2)

   pm_differences_weighted = weighted_avg_err(pm_differences_wmean.join(pm_differences_wmean_error))
   pm_differences = weighted_avg_err(pm_differences_mean.join(pm_differences_mean_error))

   table['hst_gaia_pmra_wmean'], table['hst_gaia_pmdec_wmean'] = table.relative_hst_gaia_pmra_wmean + pm_differences_weighted.pmra_wmean, table.relative_hst_gaia_pmdec_wmean + pm_differences_weighted.pmdec_wmean
   table['hst_gaia_pmra_wmean_error'], table['hst_gaia_pmdec_wmean_error'] = np.sqrt(table.relative_hst_gaia_pmra_wmean_error**2 + pm_differences_weighted.pmra_wmean_error**2), np.sqrt(table.relative_hst_gaia_pmdec_wmean_error**2 + pm_differences_weighted.pmdec_wmean_error**2)

   table['hst_gaia_pmra_mean'], table['hst_gaia_pmdec_mean'] = table.relative_hst_gaia_pmra_mean + pm_differences.pmra_mean, table.relative_hst_gaia_pmdec_mean + pm_differences.pmdec_mean
   table['hst_gaia_pmra_mean_error'], table['hst_gaia_pmdec_mean_error'] = np.sqrt(table.relative_hst_gaia_pmra_mean_error**2 + pm_differences.pmra_mean_error**2), np.sqrt(table.relative_hst_gaia_pmdec_mean_error**2 + pm_differences.pmdec_mean_error**2)

   return table


def cli_progress_test(current, end_val, bar_length=50):
   """
   Just a progress bar.
   """

   percent = float(current) / end_val
   hashes = '#' * int(round(percent * bar_length))
   spaces = ' ' * (bar_length - len(hashes))
   sys.stdout.write("\rProcessing: [{0}] {1}%".format(hashes + spaces, int(round(percent * 100))))
   sys.stdout.flush()


def add_inner_title(ax, title, loc, size=None, **kwargs):
   """
   Add text with stroke inside plots 
   """
   from matplotlib.offsetbox import AnchoredText
   from matplotlib.patheffects import withStroke

   if size is None:
       size = dict(size=plt.rcParams['legend.fontsize'])
   at = AnchoredText(title, loc=loc, prop=size,
                     pad=0., borderpad=0.5,
                     frameon=False, **kwargs)
   ax.add_artist(at)
   at.txt._text.set_path_effects([withStroke(foreground="w", linewidth=3)])
   return at


def plot_results(table, hst_image_list, HST_path, use_mean = 'wmean', plot_name_1 = 'output1', plot_name_2 = 'output2', plot_name_3 = 'output3', plot_name_4 = 'output4', ext = '.pdf'):
   """
   Plot results
   """

   pmra_lims = [table['hst_gaia_pmra_%s'%use_mean].mean()-5*table['hst_gaia_pmra_%s'%use_mean].std(), table['hst_gaia_pmra_%s'%use_mean].mean()+5*table['hst_gaia_pmra_%s'%use_mean].std()]
   pmdec_lims = [table['hst_gaia_pmdec_%s'%use_mean].mean()-5*table['hst_gaia_pmdec_%s'%use_mean].std(), table['hst_gaia_pmdec_%s'%use_mean].mean()+5*table['hst_gaia_pmdec_%s'%use_mean].std()]
   
   plt.close('all')
   
   fig, (ax1, ax2, ax3) = plt.subplots(1,3, sharex = False, sharey = False, figsize = (12, 3.5))

   ax1.plot(table.pmra[table.use_for_alignment == False], table.pmdec[table.use_for_alignment == False], 'k.', ms = 1, alpha = 0.35)
   ax1.plot(table.pmra[table.use_for_alignment == True], table.pmdec[table.use_for_alignment == True], 'k.', ms = 1)
   ax1.grid()
   ax1.set_xlabel(r'$\mu_{\alpha*}$ [m.a.s./yr.]')
   ax1.set_ylabel(r'$\mu_{\delta}$ [m.a.s./yr.]')
   try:
      ax1.set_xlim(pmra_lims)
      ax1.set_ylim(pmdec_lims)
   except:
      pass
   add_inner_title(ax1, 'Gaia' , loc=1)


   ax2.plot(table['hst_gaia_pmra_%s'%use_mean][table.use_for_alignment == False], table['hst_gaia_pmdec_%s'%use_mean][table.use_for_alignment == False], 'r.', ms =1, alpha = 0.35)
   ax2.plot(table['hst_gaia_pmra_%s'%use_mean][table.use_for_alignment == True], table['hst_gaia_pmdec_%s'%use_mean][table.use_for_alignment == True], 'r.', ms =1)
   ax2.grid()
   ax2.set_xlabel(r'$\mu_{\alpha*}$ [m.a.s./yr.]')
   ax2.set_ylabel(r'$\mu_{\delta}$ [m.a.s./yr.]')
   try:
      ax2.set_xlim(pmra_lims)
      ax2.set_ylim(pmdec_lims)
   except:
      pass
   add_inner_title(ax2, 'HST+Gaia' , loc=1)


   ax3.plot(table.gmag[table.use_for_alignment == False], np.sqrt(table.pmra_error[table.use_for_alignment == False]**2+table.pmdec_error[table.use_for_alignment == False]**2), 'k.', ms = 1, alpha = 0.35)
   ax3.plot(table.gmag[table.use_for_alignment == True], np.sqrt(table.pmra_error[table.use_for_alignment == True]**2+table.pmdec_error[table.use_for_alignment == True]**2), 'k.', ms = 1, label = 'Gaia')

   ax3.plot(table.gmag[table.use_for_alignment == False], np.sqrt(table['hst_gaia_pmra_%s_error'%use_mean][table.use_for_alignment == False]**2+table['hst_gaia_pmdec_%s_error'%use_mean][table.use_for_alignment == False]**2), 'r.', ms = 1, alpha = 0.35)
   ax3.plot(table.gmag[table.use_for_alignment == True], np.sqrt(table['hst_gaia_pmra_%s_error'%use_mean][table.use_for_alignment == True]**2+table['hst_gaia_pmdec_%s_error'%use_mean][table.use_for_alignment == True]**2), 'r.', ms = 1, label = 'Gaia+HST')

   ax3.grid()
   ax3.legend(prop={'size': 8})

   try:
      ax3.set_ylim(0, np.sqrt(table.pmra_error**2+table.pmdec_error**2).max())
   except:
      pass
   ax3.set_xlabel('Gmag')
   ax3.set_ylabel(r'$\sqrt{\sigma(\mu_{\alpha*})^2 + \sigma(\mu_{\delta})^2 }$ [m.a.s./yr.]')

   plt.subplots_adjust(wspace=0.3, hspace=0.1)

   plt.savefig(plot_name_1+ext, bbox_inches='tight')


   plt.close('all')

   fig2, (ax1, ax2, ax3) = plt.subplots(1,3, sharex = False, sharey = False, figsize = (12, 3.5))
   ax1.errorbar(table.pmra, table['hst_gaia_pmra_%s'%use_mean], xerr=table.pmra_error, yerr=table['hst_gaia_pmra_%s_error'%use_mean], fmt = '.', ms=2, color = '0.1', zorder = 1, alpha = 0.5, elinewidth = 0.5)
   ax1.plot([pmra_lims[0], pmra_lims[1]], [pmra_lims[0], pmra_lims[1]], 'r-', linewidth = 0.5)
   ax1.grid()
   ax1.set_xlabel(r'$\mu_{\alpha*, Gaia}$ [m.a.s./yr.]')
   ax1.set_ylabel(r'$\mu_{\alpha*, HST + Gaia }$ [m.a.s./yr.]')
   try:
      ax1.set_xlim(pmra_lims)
      ax1.set_ylim(pmra_lims)      
   except:
      pass

   ax2.errorbar(table.pmdec, table['hst_gaia_pmdec_%s'%use_mean], xerr=table.pmdec_error, yerr=table['hst_gaia_pmdec_%s_error'%use_mean], fmt = '.', ms=2, color = '0.1', zorder = 1, alpha = 0.5, elinewidth = 0.5)
   ax2.plot([pmdec_lims[0], pmdec_lims[1]], [pmdec_lims[0], pmdec_lims[1]], 'r-', linewidth = 0.5)
   ax2.grid()
   ax2.set_xlabel(r'$\mu_{\delta, Gaia}$ [m.a.s./yr.]')
   ax2.set_ylabel(r'$\mu_{\delta, HST + Gaia}$ [m.a.s./yr.]')
   try:
      ax2.set_xlim(pmdec_lims)
      ax2.set_ylim(pmdec_lims)      
   except:
      pass

   ax3.errorbar(table.pmra - table['hst_gaia_pmra_%s'%use_mean], table.pmdec - table['hst_gaia_pmdec_%s'%use_mean], xerr=np.sqrt(table.pmra_error**2 + table['hst_gaia_pmra_%s_error'%use_mean]**2), yerr=np.sqrt(table.pmdec_error**2 + table['hst_gaia_pmdec_%s_error'%use_mean]**2), fmt = '.', ms=2, color = '0.1', zorder = 1, alpha = 0.5, elinewidth = 0.5)
   
   ax3.axvline(x=0, color ='r', linewidth = 0.5)
   ax3.axhline(y=0, color ='r', linewidth = 0.5)

   ax3.grid()
   ax3.set_aspect('equal', adjustable='datalim')
   
   ax3.set_xlabel(r'$\mu_{\alpha*, Gaia}$ - $\mu_{\alpha*, HST + Gaia}$ [m.a.s./yr.]')
   ax3.set_ylabel(r'$\mu_{\delta, Gaia}$ - $\mu_{\delta, HST + Gaia}$ [m.a.s./yr.]')

   plt.subplots_adjust(wspace=0.3, hspace=0.1)

   plt.savefig(plot_name_2+ext, bbox_inches='tight')


   plt.close('all')

   fig2, ax = plt.subplots(1,1, sharex = False, sharey = False, figsize = (5.5, 5.5))

   hst_filters = [col for col in table.columns if ('F' in col) & ('error' not in col) & ('std' not in col) & ('_mean' not in col)]
   hst_filters.sort()
   if len(hst_filters) >= 2:
      color = (table[hst_filters[0]]-table[hst_filters[1]]).rename('%s - %s'%(hst_filters[0], hst_filters[1]))
      mag = table[hst_filters[1]]
   else:
      color = (table['gmag']-table[hst_filters[0]]).rename('Gmag - %s'%hst_filters[0])
      mag = table[hst_filters[0]]

   ax.plot(color[table.use_for_alignment == False], mag[table.use_for_alignment == False], 'k.', ms=1, alpha = 0.35)
   ax.plot(color[table.use_for_alignment == True], mag[table.use_for_alignment == True], 'k.', ms=1)

   ax.set_xlabel(color.name)
   ax.set_ylabel(mag.name)

   try:
      ax.set_xlim(np.nanmin(color)-0.1, np.nanmax(color)+0.1)
      ax.set_ylim(np.nanmax(mag)+0.25, np.nanmin(mag)-0.25)
   except:
      pass

   plt.savefig(plot_name_3+ext, bbox_inches='tight')


   plt.close('all')
   
   try:
      hdu_list = []
      for index_image, (obs_id, HST_image) in hst_image_list.loc[:, ['obs_id', 'productFilename']].iterrows():

         HST_image_filename = HST_path+'mastDownload/HST/'+obs_id+'/'+HST_image

         hdu_list.append(fits.open(HST_image_filename)[1])

      if len(hdu_list) > 1:
         from reproject.mosaicking import find_optimal_celestial_wcs
         from reproject import reproject_interp
         from reproject.mosaicking import reproject_and_coadd
         
         wcs, shape = find_optimal_celestial_wcs(hdu_list, resolution=0.5 * u.arcsec)
         image_data, image_footprint = reproject_and_coadd(hdu_list, wcs, shape_out=shape, reproject_function=reproject_interp, order = 'nearest-neighbor', match_background=True)

      else:
         wcs = WCS(hdu_list[0].header)
         image_data = hdu_list[0].data

      fig = plt.figure(figsize=(10,10),dpi=250)
      ax = fig.add_subplot(111, projection=wcs)

      norm = ImageNormalize(image_data, interval = ManualInterval(0.0,0.15))

      im = ax.imshow(image_data, cmap='gray_r', origin='lower', norm=norm)

      ax.set_xlabel("RA")
      ax.set_ylabel("Dec")

      try:
         p1 = ax.scatter(table['ra'][table.use_for_alignment == False],table['dec'][table.use_for_alignment == False], transform=ax.get_transform('world'), s=20, linewidth = 1, edgecolor='salmon', facecolor='none', label='Not used in alignment')
      except:
         pass
      try:
         with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            q1 = ax.quiver(table['ra'][table.use_for_alignment == False], table['dec'][table.use_for_alignment == False], -table['hst_gaia_pmra_%s'%use_mean][table.use_for_alignment == False], table['hst_gaia_pmdec_%s'%use_mean][table.use_for_alignment == False], transform=ax.get_transform('world'), color='salmon', width = 0.003, angles = 'xy')
      except:
         pass

      try:
         p2 = ax.scatter(table['ra'][table.use_for_alignment == True],table['dec'][table.use_for_alignment == True], transform=ax.get_transform('world'), s=20, linewidth = 1, edgecolor='limegreen', facecolor='none', label='Used in alignment')
      except:
         pass
      try:
         with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            q2 = ax.quiver(table['ra'][table.use_for_alignment == True],table['dec'][table.use_for_alignment == True], table['hst_gaia_pmra_%s'%use_mean][table.use_for_alignment == True], table['hst_gaia_pmdec_%s'%use_mean][table.use_for_alignment == True], transform=ax.get_transform('world'), color='limegreen', width = 0.003, angles = 'xy')
      except:
         pass

      ax.grid()

      if table.use_for_alignment.sum() < len(table):
         plt.legend()

      plt.savefig(plot_name_4+'.png', bbox_inches='tight')

   except:
      print('A problem was found when generating the footprint.')


def get_object_properties(args):
   """
   This routine will try to obtain all the required object properties from Simbad or from the user.
   """

   #Try to get object:
   if (args.ra is None) or (args.dec is None):
      try:
         from astroquery.simbad import Simbad
         import astropy.units as u
         from astropy.coordinates import SkyCoord

         customSimbad = Simbad()
         customSimbad.add_votable_fields('distance', 'propermotions', 'dim', 'fe_h')

         object_table = customSimbad.query_object(args.name)
         print('Object found:', object_table['MAIN_ID'])

         coo = SkyCoord(ra = object_table['RA'], dec = object_table['DEC'], unit=(u.hourangle, u.deg))

         args.ra = float(coo.ra.deg)
         args.dec = float(coo.dec.deg)

         #Try to get the search radius
         if all((args.search_radius == None, any((args.search_width == None, args.search_height == None)))):
            if (object_table['GALDIM_MAJAXIS'].mask == False):
               args.search_radius = max(np.round(float(2. * object_table['GALDIM_MAJAXIS'] / 60.), 2), 0.1)
            else:
               try:
                  args.search_radius = float(input('Search radius not defined, please enter the search radius in degrees (Press enter to adopt the default value of 1 deg): '))
               except:
                  args.search_radius = 1.0

      except:
         if args.ra is None:
            args.ra = float(input('R.A. not defined, please enter R.A. in degrees: '))
         if args.dec is None:
            args.dec = float(input('Dec not defined, please enter Dec in degrees: '))

         #Try to get radius
         if args.search_radius is None:
            try:
               args.search_radius = float(input('Search radius not defined, please enter the search radius in degrees (Press enter to adopt the default value of 1 deg): '))
            except:
               args.search_radius = 1.0

   if args.search_radius is None:
      args.search_radius = 1.0

   if (args.search_height is None):
      try:
         args.search_height = 2.*args.search_radius
      except:
         args.search_height = 1.0
   if (args.search_width is None):
      try:
         args.search_width = np.abs(2.*args.search_radius/np.cos(np.deg2rad(args.dec)))
      except:
         args.search_width = 1.0

   setattr(args, 'area', args.search_height * args.search_width * np.abs(np.cos(np.deg2rad(args.dec))))
   setattr(args, 'download_radius', args.search_radius)

   if args.error_weighted:
      args.use_mean = 'wmean'
   else:
      args.use_mean = 'mean'

   name_coo = 'ra_%.3f_dec_%.3f_r_%.2f'%(args.ra, args.dec, args.search_radius)

   if args.name is not None:
      args.name = args.name.replace(" ", "_")
      args.base_file_name = args.name+'_'+name_coo
   else:
      args.name = name_coo
      args.base_file_name = name_coo

   #The script creates directories and set files names
   args.base_path = './%s/'%(args.name)
   args.HST_path = args.base_path+'HST/'
   args.Gaia_path = args.base_path+'Gaia/'
   args.Gaia_ind_queries_path = args.Gaia_path+'individual_queries/'
   
   args.used_HST_obs_table_filename = args.base_path + args.base_file_name+'_used_HST_images.csv'
   args.HST_Gaia_table_filename = args.base_path + args.base_file_name+'.csv'
   args.logfile = args.base_path + args.base_file_name+'.log'
   args.queries = args.Gaia_path + args.base_file_name+'_queries.log'
   
   args.Gaia_raw_table_filename = args.Gaia_path + args.base_file_name+'_raw.csv'
   args.Gaia_raw_sel_table_filename = args.Gaia_path + args.base_file_name+'_raw_selection.csv'
   args.HST_obs_table_filename = args.HST_path + args.base_file_name+'_obs.csv'
   args.HST_data_table_products_filename = args.HST_path + args.base_file_name+'_data_products.csv'

   args.date_second_epoch = Time('%4i-%02i-%02iT00:00:00.000'%(args.date_second_epoch[2], args.date_second_epoch[0], args.date_second_epoch[1])).mjd
   args.date_reference_second_epoch = Time(args.date_reference_second_epoch)

   print('\n')
   print(' USED PARAMETERS '.center(42, '*'))
   print('- (ra, dec) = (%s, %s) deg.'%(round(args.ra, 5), round(args.dec, 5)))
   print('- radius = %s deg.'%args.search_radius)
   print('*'*42+'\n')

   return args


def str2bool(v):
   """
   This routine converts ascii input to boolean.
   """
 
   if v.lower() in ('yes', 'true', 't', 'y'):
      return True
   elif v.lower() in ('no', 'false', 'f', 'n'):
      return False
   else:
      raise argparse.ArgumentTypeError('Boolean value expected.')


def main(argv):  
   """
   Inputs
   """

   parser = argparse.ArgumentParser(description="This script derives proper motions (PM) combining HST and Gaia data.")
   
   # Search options
   parser.add_argument('--name', type=str, default = 'Output', help='Name for the Output table.')
   parser.add_argument('--ra', type=float, default = None, help='Central R.A.')
   parser.add_argument('--dec', type=float, default = None, help='Central Dec.')
   parser.add_argument('--search_radius', type=float, default = None, help='Radius of search in degrees.')
   parser.add_argument('--search_width', type=float, default = None, help='Width of the search rectangule in degrees.')
   parser.add_argument('--search_height', type=float, default = None, help='Height of the search rectangule in degrees.')
   parser.add_argument('--min_gmag', type=float, default = 16.0, help='Brighter G magnitude')
   parser.add_argument('--max_gmag', type=float, default = 21.5, help='Fainter G magnitude')

   # Gaia options
   parser.add_argument('--source_table', type = str, default = 'gaiaedr3.gaia_source', help='Gaia source table. Default is gaiaedr3.gaia_source.')
   parser.add_argument('--save_individual_queries', type = str2bool, default = True, help='If True, the code will save the individual queries.')
   parser.add_argument('--remove_quality_cols', type = str2bool, default = False, help='If True, the code will remove all quality columns from the final table, except "clean_label".')
   parser.add_argument('--clean_data', type = str2bool, default = False, help = 'Screen out bad measurements based on Gaia EDR3 quality flags. Default is False.')
   parser.add_argument('--sigma_flux_excess_factor', type=float, default=3., help='Sigma used for clipping in flux_excess_factor. Default is 3.')
   parser.add_argument('--only_5p_solutions', type = str2bool, default = False, help='If True, only 5p solution stars will be considered as "good" sources. Default is False.')
   parser.add_argument('--date_second_epoch', type=int, nargs='+', default= [5, 28, 2017], help='Second epoch adquisition date. Default is Gaia EDR3 (05-28-2017).')
   parser.add_argument('--date_reference_second_epoch', type=str, default= 'J2016.0', help='Second epoch reference date. Default is Gaia EDR3 J2016.0.')

   # HST options
   parser.add_argument('--force_hst1pass', type=str2bool, default=False, help='Force the program to perform the search for sources in the HST images. Default is False, which will use existing files if any.')
   parser.add_argument('--force_fmin', type=int, default= None, help='Minimum flux above the sky to substract a source in the HST image. Default is automatic, computed from the HST integration time.')
   parser.add_argument('--force_pixel_scale', type=float, default= None, help='Pixel scale in arcsec/pixel used to compute the tangential plane during the match between epochs. Default is automatic, and will take the value from the HST images.')
   parser.add_argument('--hst_filters', type=str, nargs='+', default = ['any'], help='Required filter for the HST images. Default all filters. They can be added as a list, e.g. "F814W" "F606W".')
   parser.add_argument('--hst_integration_time_min', type=float, default = 50, help='Required minimum average integration time for a set of HST images. This quantity is a limit on the average exposure time of an entire set of observations. Therefore, longer and shorter exposures may be available in an specific data set.')
   parser.add_argument('--hst_integration_time_max', type=float, default = 2000, help='Required maximum average integration time for a set of HST images. This quantity is a limit on the average exposure time of an entire set of observations. Therefore, longer and shorter exposures may be available in an specific data set. Expossures with less that 500 seconds of integration time are preferred.')
   parser.add_argument('--time_baseline', type=float, default = 2190, help='Minimum time baseline with respect to Gaia EDR3 in days. Default 2190.')
   parser.add_argument('--project', type=str, nargs='+', default = ['HST'], help='Processing project. E.g. HST, HLA, EUVE, hlsp_legus. Default HST. They can be added as a list, e.g. "HST", "HLA".')

   # HST-Gaia match options
   parser.add_argument('--use_members', type=str2bool, default=True, help='Whether to use only member stars for the epochs alignment or to use all available stars.')
   parser.add_argument('--clipping_prob_pm', type=float, default=3., help='Sigma used for clipping pm and parallax. Default is 3.')
   parser.add_argument('--pm_n_components', type=int, default=2, help='Number of Gaussian componnents for pm and parallax clustering. Default is 1.')
   parser.add_argument('--force_xym2pm', type=str2bool, default=True, help='Force the program to perform the match between Gaia and HST sources. Default is True. If False, it will use existing files if any.')
   parser.add_argument('--rewind_stars', type=str2bool, default=True, help='Force the program to use the PMs to rewind the stars to their second epoch to the first one before the matching.')
   parser.add_argument('--fix_mat', type=str2bool, default=False, help='Force the program to use only the best stars in the MAT files to perform the alignment in the next iteration.')
   parser.add_argument('--max_separation', type=float, default= None, help='Maximum allowed separation in pixels during the match between epochs. Default 10 pixels.')
   parser.add_argument('--force_use_sat', type=str2bool, default=True, help='Force the program to use saturated stars during the match between epochs. Default is True.')
   parser.add_argument('--force_wcs_search_radius', type=float, default= None, help='When set to a radius (in arcsec), the program search the closest Gaia star to each bright star in the HST image within that distance to perform a pre-alignment between the two frames. Useful when not many stars are available. Default is None.')
   parser.add_argument('--min_stars_alignment', type=int, default = 10, help='Minimum number of stars per HST image to be used for the epochs alignment. Default 10.')
   parser.add_argument('--force_use_amp', type=str2bool, default=True, help='Force the program to use amplifier-based transformations. Default is False.')
   parser.add_argument('--min_stars_amp', type=int, default = 10, help='Minimum number of stars per HST amplifier to compute transformations. Default is 10.')

   #Miscellaneus options
   parser.add_argument('--use_parallel', type = str2bool, default = True, help='Use parallelized computation when possible. Default is True.')
   parser.add_argument('--load_existing', type = str2bool, default = False, help='If True, the code will try to resume the previous Gaia search and load previous individual queries. It should be set to False if a new table is being downloaded. True when a specific search is failing due to connection problems.')
   parser.add_argument('--plots', type=str2bool, default=True, help='Create sanity plots. Default is True.')
   parser.add_argument('--error_weighted', type = str2bool, default = True, help = 'The program will use error-weighted statistics to compute average PMs, if possible.')
   parser.add_argument('--remove_previous_files', type=str2bool, default=True, help='Remove previous intermediate files.')
   parser.add_argument('--verbose', type=str2bool, default=True, help='Controls the program verbosity. Default True.')

   args = parser.parse_args(argv)
   args = get_object_properties(args)

   """
   The script creates directories and set files names
   """

   create_dir(args.base_path)
   create_dir(args.HST_path)
   create_dir(args.Gaia_path)
   if args.save_individual_queries:
      create_dir(args.Gaia_ind_queries_path)

   """
   The script tries to load an existing Gaia table, otherwise it will download it from the Gaia archive.
   """

   astrometric_cols = 'l, b, ra, ra_error, dec, dec_error, parallax, parallax_error, pmra, pmra_error, pmdec, pmdec_error, dr2_radial_velocity, dr2_radial_velocity_error, ra_dec_corr, ra_parallax_corr, ra_pmra_corr, ra_pmdec_corr, dec_parallax_corr, dec_pmra_corr, dec_pmdec_corr, parallax_pmra_corr, parallax_pmdec_corr, pmra_pmdec_corr'

   photometric_cols = 'phot_g_mean_flux, phot_g_mean_mag AS gmag, (1.086*phot_g_mean_flux_error/phot_g_mean_flux) AS gmag_error, phot_bp_mean_mag AS bpmag, (1.086*phot_bp_mean_flux_error/phot_bp_mean_flux) AS bpmag_error, phot_rp_mean_mag AS rpmag, (1.086*phot_rp_mean_flux_error/phot_rp_mean_flux) AS rpmag_error, bp_rp, sqrt( power( (1.086*phot_bp_mean_flux_error/phot_bp_mean_flux), 2) + power( (1.086*phot_rp_mean_flux_error/phot_rp_mean_flux), 2) ) as bp_rp_error'

   quality_cols = 'ecl_lat, pseudocolour, nu_eff_used_in_astrometry, visibility_periods_used, astrometric_excess_noise_sig, astrometric_params_solved, astrometric_n_good_obs_al, astrometric_chi2_al, phot_bp_rp_excess_factor, ruwe, (phot_bp_n_blended_transits+phot_rp_n_blended_transits) *1.0 / (phot_bp_n_obs + phot_rp_n_obs) AS beta, ipd_gof_harmonic_amplitude, phot_bp_n_contaminated_transits, phot_rp_n_contaminated_transits'

   query, quality_cols = columns_n_conditions(args.source_table, astrometric_cols, photometric_cols, quality_cols, args.ra, args.dec, args.search_width, args.search_height)

   try:
      Gaia_table = pd.read_csv(args.Gaia_raw_table_filename)
   except:
      Gaia_table, Gaia_queries = incremental_query(query, args.area, min_gmag = args.min_gmag, max_gmag = args.max_gmag, use_parallel = args.use_parallel,
                                                   save_individual_queries = args.save_individual_queries, name = args.name)

      Gaia_table.to_csv(args.Gaia_raw_table_filename, index = False)

      f = open(args.queries, 'w+')
      if type(Gaia_queries) is list:
         for Gaia_query in Gaia_queries:
            f.write('%s\n'%Gaia_query)
            f.write('\n')
      else:
         f.write('%s\n'%Gaia_queries)
      f.write('\n')
      f.close()


   # We fix some of the variables using the codes published with EDR3
   Gaia_table['corrected_flux_excess_factor'] = correct_flux_excess_factor(Gaia_table['bp_rp'], Gaia_table['phot_bp_rp_excess_factor'])

   clean_label = pre_clean_data(Gaia_table['gmag'], Gaia_table['corrected_flux_excess_factor'], Gaia_table['ruwe'], Gaia_table['ipd_gof_harmonic_amplitude'], Gaia_table['visibility_periods_used'], Gaia_table['astrometric_excess_noise_sig'], Gaia_table['astrometric_params_solved'], sigma_flux_excess_factor = args.sigma_flux_excess_factor, use_5p = args.only_5p_solutions)

   Gaia_table['clean_label'] = clean_label

   Gaia_table = get_real_error(Gaia_table)

   Gaia_table.to_csv(args.Gaia_raw_sel_table_filename)

   if args.clean_data:
      Gaia_table = Gaia_table[Gaia_table.clean_label == True]

   """
   The script tries to load an existing HST table, otherwise it will download it from the MAST archive.
   """
   obs_table, data_products_by_obs = search_mast(args.ra, args.dec, args.search_width, args.search_height, filters = args.hst_filters, project = args.project, t_exptime_min = args.hst_integration_time_min, t_exptime_max = args.hst_integration_time_max, date_second_epoch = args.date_second_epoch, time_baseline = args.time_baseline)

   obs_table.to_csv(args.HST_obs_table_filename, index = False)
   data_products_by_obs.to_csv(args.HST_data_table_products_filename, index = False)

   """
   Plot results and find Gaia stars within HST fields
   """
   Gaia_table, obs_table = plot_fields(Gaia_table, obs_table, args.HST_path, min_stars_alignment = args.min_stars_alignment, name = args.base_path+args.base_file_name+'_search_footprint.png')

   if len(obs_table) > 0:

      """
      Ask whether the user wish to download the available HST images 
      """

      print('Would you like to use the following HST observations?\n')
      print(obs_table.loc[:, ['obsid', 'filters', 'n_exp', 'i_exptime', 'obs_time', 't_baseline', 'gaia_stars_per_obs', 'proposal_id', '']].to_string(index=False), '\n')

      print("Type 'y' for all observations, 'n' for none. Type the number within parentheses at the right if you wish to use that specific set of observations. You can enter several numbers separated by space. \n")
      HST_obs_to_use = input('Please type your answer and press enter: ')
      print('\n')

      try:
         HST_obs_to_use = str2bool(HST_obs_to_use)
      except:
         try:
            HST_obs_to_use = list(set([obsid for obsid in obs_table.obsid[[int(obsid)-1 for obsid in HST_obs_to_use.split()]] if np.isfinite(obsid)]))
         except:
            print('No valid input. Not downloading observations.')
            HST_obs_to_use = False


      if HST_obs_to_use is not False:
         if HST_obs_to_use is True:
            HST_obs_to_use = list(obs_table['obsid'].values)
         hst_images = download_HST_images(Table.from_pandas(data_products_by_obs.loc[data_products_by_obs['parent_obsid'].isin(HST_obs_to_use), :]), path = args.HST_path)
      else:
         print('\nExiting now.\n')
         sys.exit(1)

      """
      Select only flc
      """
      drz_images = data_products_by_obs[(data_products_by_obs['productSubGroupDescription'] == 'DRZ') & (data_products_by_obs['parent_obsid'].isin(HST_obs_to_use))]
      flc_images = data_products_by_obs[(data_products_by_obs['productSubGroupDescription'] == 'FLC') & (data_products_by_obs['parent_obsid'].isin(HST_obs_to_use))]

      """
      Call hst1pass
      """
      launch_hst1pass(flc_images, HST_obs_to_use, args.HST_path, force_fmin = args.force_fmin, remove_previous_XYmqxyrd = args.remove_previous_files, force_hst1pass = args.force_hst1pass, use_parallel = args.use_parallel)
      """
      Call xym2pm_Gaia
      """
      Gaia_table_hst = launch_xym2pm_Gaia(Gaia_table.copy(), flc_images, HST_obs_to_use, args.HST_path, args.date_reference_second_epoch, only_use_members = args.use_members, rewind_stars = args.rewind_stars, force_pixel_scale = args.force_pixel_scale, force_max_separation = args.max_separation, force_use_sat = args.force_use_sat, fix_mat = args.fix_mat, force_use_amp = args.force_use_amp, min_stars_amp = args.min_stars_amp, force_wcs_search_radius = args.force_wcs_search_radius, n_components = args.pm_n_components, clipping_prob = args.clipping_prob_pm, min_stars_alignment = args.min_stars_alignment, use_mean = args.use_mean, plots = args.plots, verbose = args.verbose, force_xym2pm = args.force_xym2pm, remove_previous_files = args.remove_previous_files, use_parallel = args.use_parallel, plot_name = args.base_path+'Match')

      """
      Save Gaia and HST tables
      """
      obs_table.to_csv(args.HST_obs_table_filename, index = False)
      data_products_by_obs.to_csv(args.HST_data_table_products_filename, index = False)

      flc_images.to_csv(args.used_HST_obs_table_filename, index = False)
      Gaia_table_hst.to_csv(args.HST_Gaia_table_filename, index = False)

      avg_pm = weighted_avg_err(Gaia_table_hst.loc[Gaia_table_hst.use_for_alignment, ['hst_gaia_pmra_%s'%args.use_mean, 'hst_gaia_pmdec_%s'%args.use_mean, 'hst_gaia_pmra_%s_error'%args.use_mean, 'hst_gaia_pmdec_%s_error'%args.use_mean]])

      """
      Print a summary with the location of files and plot the results
      """
      plot_results(Gaia_table_hst, drz_images, args.HST_path, use_mean = args.use_mean, plot_name_1 = args.base_path+args.base_file_name+'_vpd', plot_name_2 = args.base_path+args.base_file_name+'_diff', plot_name_3 = args.base_path+args.base_file_name+'_cmd', plot_name_4 = args.base_path+args.base_file_name+'_footprint', ext = '.pdf')

      logresults = ' RESULTS '.center(82, '-')+'\n - Final table: %s'%args.HST_Gaia_table_filename+'\n - Used HST observations: %s'%args.used_HST_obs_table_filename+'\n'+'-'*82+'\n - A total of %i stars were used.\n'%Gaia_table_hst.use_for_alignment.sum() +' - Average absolute PM of used stars: \n   pmra = %s+-%s \n'%(round_significant(avg_pm['hst_gaia_pmra_%s_%s'%(args.use_mean, args.use_mean)], avg_pm['hst_gaia_pmra_%s_%s_error'%(args.use_mean, args.use_mean)]))+'   pmdec = %s+-%s \n '%(round_significant(avg_pm['hst_gaia_pmdec_%s_%s'%(args.use_mean, args.use_mean)], avg_pm['hst_gaia_pmdec_%s_%s_error'%(args.use_mean, args.use_mean)]))+'-'*82 + '\n \n Execution ended.\n'

      print('\n')
      print(logresults)

      f = open(args.logfile, 'w+')
      f.write(logresults)
      f.close()

   else:
      input('No suitable HST observations were found. Please try with different parameters.\nPress enter to exit.\n')

if __name__ == '__main__':
    main(sys.argv[1:])
    sys.exit(0)

"""
Andres del Pino Molina
"""

