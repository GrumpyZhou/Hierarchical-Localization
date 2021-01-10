import os
import time
import argparse
from pathlib import Path
import numpy as np
import h5py
from scipy.io import loadmat
import torch
from tqdm import tqdm
import logging
import pickle
import cv2
import pycolmap

from .utils.parsers import parse_retrieval, names_to_pair


def interpolate_scan(scan, kp):
    h, w, c = scan.shape
    kp = kp / np.array([[w-1, h-1]]) * 2 - 1
    assert np.all(kp > -1) and np.all(kp < 1)
    scan = torch.from_numpy(scan).permute(2, 0, 1)[None]
    kp = torch.from_numpy(kp)[None, None]
    grid_sample = torch.nn.functional.grid_sample

    # To maximize the number of points that have depth:
    # do bilinear interpolation first and then nearest for the remaining points
    interp_lin = grid_sample(
        scan, kp, align_corners=True, mode='bilinear')[0, :, 0]
    interp_nn = torch.nn.functional.grid_sample(
        scan, kp, align_corners=True, mode='nearest')[0, :, 0]
    interp = torch.where(torch.isnan(interp_lin), interp_nn, interp_lin)
    valid = ~torch.any(torch.isnan(interp), 0)

    kp3d = interp.T.numpy()
    valid = valid.numpy()
    return kp3d, valid


def get_scan_pose(dataset_dir, rpath):
    split_image_rpath = rpath.split('/')
    floor_name = split_image_rpath[-3]
    scan_id = split_image_rpath[-2]
    image_name = split_image_rpath[-1]
    building_name = image_name[:3]

    path = Path(
        dataset_dir, 'database/alignments', floor_name,
        f'transformations/{building_name}_trans_{scan_id}.txt')
    with open(path) as f:
        raw_lines = f.readlines()

    P_after_GICP = np.array([
        np.fromstring(raw_lines[7], sep=' '),
        np.fromstring(raw_lines[8], sep=' '),
        np.fromstring(raw_lines[9], sep=' '),
        np.fromstring(raw_lines[10], sep=' ')
    ])

    return P_after_GICP


def pose_from_cluster(dataset_dir, q, retrieved, feature_file, match_file,
                      skip=None):
    height, width = cv2.imread(str(dataset_dir / q)).shape[:2]
    cx = .5 * width
    cy = .5 * height
    focal_length = 4032. * 28. / 36.

    all_mkpq = []
    all_mkpr = []
    all_mkp3d = []
    all_indices = []
    kpq = feature_file[q]['keypoints'].__array__()
    num_matches = 0

    for i, r in enumerate(retrieved):
        kpr = feature_file[r]['keypoints'].__array__()
        pair = names_to_pair(q, r)
        m = match_file[pair]['matches0'].__array__()
        v = (m > -1)

        if skip and (np.count_nonzero(v) < skip):
            continue

        mkpq, mkpr = kpq[v], kpr[m[v]]
        num_matches += len(mkpq)

        scan_r = loadmat(Path(dataset_dir, r + '.mat'))["XYZcut"]
        mkp3d, valid = interpolate_scan(scan_r, mkpr)
        Tr = get_scan_pose(dataset_dir, r)
        mkp3d = (Tr[:3, :3] @ mkp3d.T + Tr[:3, -1:]).T

        all_mkpq.append(mkpq[valid])
        all_mkpr.append(mkpr[valid])
        all_mkp3d.append(mkp3d[valid])
        all_indices.append(np.full(np.count_nonzero(valid), i))

    all_mkpq = np.concatenate(all_mkpq, 0)
    all_mkpr = np.concatenate(all_mkpr, 0)
    all_mkp3d = np.concatenate(all_mkp3d, 0)
    all_indices = np.concatenate(all_indices, 0)

    cfg = {
        'model': 'SIMPLE_PINHOLE',
        'width': width,
        'height': height,
        'params': [focal_length, cx, cy]
    }
    ret = pycolmap.absolute_pose_estimation(
        all_mkpq, all_mkp3d, cfg, 48.00)
    ret['cfg'] = cfg
    return ret, all_mkpq, all_mkpr, all_mkp3d, all_indices, num_matches

def pose_from_cluster_onfly_matching(dataset_dir, q, retrieved, 
                                     matcher, poses, rthres, skip_matches):
    """Instead of loading prestored keypoints and matches, as in 
    pose_from_cluster(), this function allows one to perform matching 
    on fly with a predefined matcher.    
    """    
    imq_path = os.path.join(dataset_dir, q)    
    height, width = cv2.imread(imq_path).shape[:2]
    focal = 4032. * 28. / 36.  
    cfg = {
        'model': 'SIMPLE_PINHOLE',
        'width': width,
        'height': height,
        'params': [focal, .5 * width, .5 * height]
    }
    
    all_mkpq = []
    all_mkpr = []
    all_mkp3d = []
    all_indices = []
    num_matches = 0
    times = []
    failed = []
    for i, r in enumerate(retrieved):
        pair = names_to_pair(q, r)
        imr_path = os.path.join(dataset_dir, r)       
        
        try:
            # Matching
            t0 = time.time()
            match_res = matcher(imq_path, imr_path)
            times.append(time.time() - t0)
            matches = match_res[0]
            
            if len(matches) < skip_matches:
                failed.append((q, r))
                continue

            mkpq, mkpr = matches[:, :2], matches[:, 2:]
            scan_r = loadmat(os.path.join(dataset_dir, r + '.mat'))["XYZcut"]
            mkp3d, valid = interpolate_scan(scan_r, mkpr)
            Tr = get_scan_pose(dataset_dir, r)
            mkp3d = (Tr[:3, :3] @ mkp3d.T + Tr[:3, -1:]).T
        except:
            failed.append((q, r))
            continue
            
        num_matches += len(mkpq)
        all_mkpq.append(mkpq[valid])
        all_mkpr.append(mkpr[valid])
        all_mkp3d.append(mkp3d[valid])
        all_indices.append(np.full(np.count_nonzero(valid), i))
    if num_matches == 0:
        return None, failed, times
    all_mkpq = np.concatenate(all_mkpq, 0)
    all_mkpr = np.concatenate(all_mkpr, 0)
    all_mkp3d = np.concatenate(all_mkp3d, 0)
    all_indices = np.concatenate(all_indices, 0)

    ret = pycolmap.absolute_pose_estimation(all_mkpq, all_mkp3d, cfg, rthres)
    ret['cfg'] = cfg        
    poses[q] = (ret['qvec'], ret['tvec'])
    res_dict =  {
            'db': retrieved,
            'PnP_ret': ret,
            'keypoints_query': all_mkpq,
            'keypoints_db': all_mkpr,
            '3d_points': all_mkp3d,
            'indices_db': all_indices,
            'num_matches': num_matches,            
    }
    return res_dict, failed, times

def localize_with_matcher(matcher, dataset_dir, retrieval_pairs, 
                          odir, method_tag, rthres=48, skip_matches=20):
    
    # Query and retrieval pairs
    retrieval_dict = parse_retrieval(retrieval_pairs)
    queries = list(retrieval_dict.keys())
    
    # Output paths: result file and log for visualization
    logging.info('Output dir: ' + odir)
    if not os.path.exists(odir):
        os.makedirs(odir) 
    res_path = os.path.join(odir, f'{method_tag}.txt')        
    logs_path = os.path.join(odir, f'{method_tag}.txt_logs.pkl')

    # Initialize log data    
    logs = {'odir': odir,
            'method': method_tag,
            'pairs': retrieval_pairs,
            'skip_matches' : skip_matches,
            'rthres': rthres,        
            'loc': {}, 
            'failed':[]
           }

    logging.info('Starting localization...')
    poses = {}
    match_times = []
    num_3d = []
    irat = []
    for q in tqdm(queries):
        retrieved = retrieval_dict[q]
        res_dict, failed, times = pose_from_cluster_onfly_matching(
            dataset_dir, q, retrieved, matcher, poses, 
            rthres=rthres, skip_matches=skip_matches
        )
        logs['loc'][q] = res_dict
        logs['failed'] += failed
        match_times += times
        if res_dict:
            num_3d.append(len(res_dict['keypoints_query']))
            irat.append(np.mean(res_dict['PnP_ret']['inliers']))
    logging.info('\nDone! Failed pairs: {} Match time: {:.2f}s Npts: {:.0f} irat:{:.2f}%'.format(len(logs['failed']), np.mean(match_times), np.mean(num_3d), 100 * np.mean(irat)))
    logging.info(f'Writing poses to {odir}...')
    with open(res_path, 'w') as f:
        for q in queries:
            if q not in poses:
                continue
            qvec, tvec = poses[q]
            qvec = ' '.join(map(str, qvec))
            tvec = ' '.join(map(str, tvec))
            name = q.split("/")[-1]
            f.write(f'{name} {qvec} {tvec}\n')
    logging.info(f'Writing logs to {logs_path}...')
    with open(logs_path, 'wb') as f:
        pickle.dump(logs, f)
    logging.info('Done!')

def main(dataset_dir, retrieval, features, matches, results,
         skip_matches=None):

    assert retrieval.exists(), retrieval
    assert features.exists(), features
    assert matches.exists(), matches

    retrieval_dict = parse_retrieval(retrieval)
    queries = list(retrieval_dict.keys())

    feature_file = h5py.File(features, 'r')
    match_file = h5py.File(matches, 'r')

    poses = {}
    logs = {
        'features': features,
        'matches': matches,
        'retrieval': retrieval,
        'loc': {},
    }
    logging.info('Starting localization...')
    for q in tqdm(queries):
        db = retrieval_dict[q]
        ret, mkpq, mkpr, mkp3d, indices, num_matches = pose_from_cluster(
            dataset_dir, q, db, feature_file, match_file, skip_matches)

        poses[q] = (ret['qvec'], ret['tvec'])
        logs['loc'][q] = {
            'db': db,
            'PnP_ret': ret,
            'keypoints_query': mkpq,
            'keypoints_db': mkpr,
            '3d_points': mkp3d,
            'indices_db': indices,
            'num_matches': num_matches,
        }

    logging.info(f'Writing poses to {results}...')
    with open(results, 'w') as f:
        for q in queries:
            qvec, tvec = poses[q]
            qvec = ' '.join(map(str, qvec))
            tvec = ' '.join(map(str, tvec))
            name = q.split("/")[-1]
            f.write(f'{name} {qvec} {tvec}\n')

    logs_path = f'{results}_logs.pkl'
    logging.info(f'Writing logs to {logs_path}...')
    with open(logs_path, 'wb') as f:
        pickle.dump(logs, f)
    logging.info('Done!')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset_dir', type=Path, required=True)
    parser.add_argument('--retrieval', type=Path, required=True)
    parser.add_argument('--features', type=Path, required=True)
    parser.add_argument('--matches', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--skip_matches', type=int)
    args = parser.parse_args()
    main(**args.__dict__)
