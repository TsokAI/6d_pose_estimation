import torch
from torch.autograd import Variable
import torch.nn.functional as F
import torchvision.transforms as transforms

import torch.nn as nn
import torch.utils.data
import numpy as np
from opt import opt

from dataloader import ImageLoader, DetectionLoader, DetectionProcessor, DataWriter, Mscoco
from yolo.util import write_results, dynamic_write_results
from SPPE.src.main_fast_inference import *

import yaml
import os
import sys
from tqdm import tqdm
import time
from fn import getTime
from utils.model import * # 3D model class
from utils.sixd import load_sixd
from utils.metrics import *

from pPose_nms import pose_nms, write_json
from IPython import embed
args = opt
args.dataset = 'coco'
TOTAL_KP_NUMBER = args.nClasses
if not args.sp:
    torch.multiprocessing.set_start_method('forkserver', force=True)
    torch.multiprocessing.set_sharing_strategy('file_system')

''' 
    Load cam, model and KP model*******************************************************
'''
class Benchmark:
    def __init__(self):
        self.cam = np.identity(3)
        self.models = {}
        self.kpmodels = {}

def load_yaml(path):
    with open(path, 'r') as f:
        content = yaml.load(f)
        return content

def load_sixd_models(base_path, obj_id):
    # This function is used to load sixd benchmark info including camera, model and kp_model.
    print("Loading models and KP models...")
    bench = Benchmark()
    bench.scale_to_meters = 0.001 # Unit in model is mm
    # You need to give camera info manually here.
    bench.cam = np.array([[572.4114, 0.0, 325.2611], [0.0, 573.57043, 242.04899], [0.0, 0.0, 1.0]])
    
    #collect model info
    model_info = load_yaml(os.path.join(base_path, 'models', 'models_info.yml'))
    for key, val in model_info.items():
        name = '{:02d}'.format(int(key))
        bench.models[name] = Model3D()
        bench.models[name].diameter = val['diameter']

    # loading models, Linemod has 15 seqs, we use 13(except 3 and 7)
    for ID in range(obj_id, obj_id + 1):
        name = 'obj_{:02d}'.format(ID)
        # embed()
        bench.models['{:02d}'.format(ID)].load(os.path.join(base_path, 'models/' + name + '.ply'), scale=bench.scale_to_meters)
    print("Loading models finished!")

    # loading and refine kp models
    ID = obj_id
    name = 'obj_{:02d}'.format(ID)
    bench.kpmodels['{:02d}'.format(ID)] = Model3D()
    bench.kpmodels['{:02d}'.format(ID)].load(os.path.join(base_path, 'kpmodels/' + name + '.ply'), scale=bench.scale_to_meters)
    bench.kpmodels['{:02d}'.format(ID)].refine(TOTAL_KP_NUMBER, save=True) # delete too close points

    print("Load and refine KP models finished!")
    return bench

if __name__ == "__main__":
    # Loading camera, model, kp_model information of SIXD benchmark datasets
    print ("Betapose from MVIG begin running now.")
    obj_id = 1
    sixd_base = "/media/data_2/SIXD/hinterstoisser"
    sixd_bench = load_sixd_models(sixd_base, obj_id)
    cam_K = sixd_bench.cam
    models = sixd_bench.models
    kpmodels = sixd_bench.kpmodels
    kp_model_vertices = kpmodels['{:02d}'.format(int(obj_id))].vertices # used in pnp
    model_vertices = models['{:02d}'.format(int(obj_id))].vertices # used in calculating add


    inputpath = args.inputpath
    inputlist = args.inputlist
    mode = args.mode
    if not os.path.exists(args.outputpath):
        os.mkdir(args.outputpath)

    if len(inputlist):
        im_names = open(inputlist, 'r').readlines()
    elif len(inputpath) and inputpath != '/':
        for root, dirs, files in os.walk(inputpath):
            im_names = files
    else:
        raise IOError('Error: must contain either --indir/--list')

    # Load input images meanwhile start processes, threads
    data_loader = ImageLoader(im_names, batchSize=args.detbatch, format='yolo', reso=int(args.inp_dim)).start()
  
    # Load detection loader
    print('Loading YOLO model..')
    sys.stdout.flush() # for multithread displaying
    det_loader = DetectionLoader(data_loader, batchSize=args.detbatch).start()
    det_processor = DetectionProcessor(det_loader).start()
    
    # Load pose model
    pose_dataset = Mscoco() # is_train, res, joints, rot_factor
    if args.fast_inference:
        pose_model = InferenNet_fast(4 * 1 + 1, pose_dataset)
    else:
        pose_model = InferenNet(4 * 1 + 1, pose_dataset)
    pose_model.cuda()
    pose_model.eval()

    runtime_profile = {
        'dt': [],
        'pt': [],
        'pn': []
    }

    # Init data writer for writing data and post
    writer = DataWriter(cam_K, kp_model_vertices, args.save_video).start() # save_video default: False

    data_len = data_loader.length()
    im_names_desc = tqdm(range(data_len))

    batchSize = args.posebatch
    for i in im_names_desc:
    # for i in range(data_len):
        # if i>10: break # for debugging
        start_time = getTime()
        with torch.no_grad():
            # Detection is handling here
            (inps, orig_img, im_name, boxes, scores, pt1, pt2) = det_processor.read()

            if boxes is None or boxes.nelement() == 0:
                writer.save(None, None, None, None, None, orig_img, im_name.split('/')[-1])
                continue

            ckpt_time, det_time = getTime(start_time)
            runtime_profile['dt'].append(det_time)
            # Pose Estimation
            datalen = inps.size(0)
            leftover = 0
            if (datalen) % batchSize: # left some
                leftover = 1
            num_batches = datalen // batchSize + leftover
            hm = []
            # embed()
            for j in range(num_batches):
                inps_j = inps[j*batchSize:min((j + 1)*batchSize, datalen)].cuda()
                # Critical, apply pose_model
                hm_j = pose_model(inps_j) #hm_j is a heatmap with size B*KP*H*W
                hm.append(hm_j)
            hm = torch.cat(hm)
            ckpt_time, pose_time = getTime(ckpt_time)
            runtime_profile['pt'].append(pose_time)
            hm = hm.cpu() # hm is torch.tensor
            writer.save(boxes, scores, hm, pt1, pt2, orig_img, im_name.split('/')[-1])
            
            ckpt_time, post_time = getTime(ckpt_time)
            runtime_profile['pn'].append(post_time)
        
        if args.profile:# False
            # TQDM
            im_names_desc.set_description(
            'det time: {dt:.3f} | pose time: {pt:.2f} | post processing: {pn:.4f}'.format(
                dt=np.mean(runtime_profile['dt']), pt=np.mean(runtime_profile['pt']), pn=np.mean(runtime_profile['pn']))
            )

    print('===========================> Finish Model Running.')
    if (args.save_img or args.save_video) and not args.vis_fast:
        print('===========================> Rendering remaining images in the queue...')
        print('===========================> If this step takes too long, you can enable the --vis_fast flag to use fast rendering (real-time).')
    while(writer.running()):
        pass
    writer.stop()
    final_result = writer.results()
    # Till now all the results from detections are in final_result.
    # # Output results in json file
    # write_json(final_result, args.outputpath)
    ''' 
        Evaluate final_result*******************************************************
    '''
    print ("Loading ground truth benchmark dataset...")
    bench_info = load_sixd(sixd_base, seq=obj_id, nr_frames=0)
    diameter = bench_info.diameter[obj_id]
    frames_of_ground_truth = bench_info.frames
    # Metrics Initialization
    add_errs = []
    adds = []
    for f in final_result:
        imgname = f['imgname']
        imgname = int(imgname[0:-4]) # throw '.png'
        gt_frame = frames_of_ground_truth[imgname]
        assert imgname == gt_frame.nr
        ground_truths = gt_frame.gt[0]
        gt_obj_id = ground_truths[0]
        gt_pose = np.array(ground_truths[1])
        gt_bbox = ground_truths[2] # [xmin, ymin, w, h]
        gt_bbox[2] += gt_bbox[0]
        gt_bbox[3] += gt_bbox[1]
        pred_cam_R = f['cam_R']
        pred_cam_t = f['cam_t']
        pred_pose = np.eye(4)
        # embed()
        if len(pred_cam_R) < 1: 
            if iou(gt_bbox, pred_bbox) >= 0.5:
                adds.append(0)
        else:
            pred_pose[:3, :3] = pred_cam_R
            pred_pose[:3, 3] = pred_cam_t[:,0]
            pred_bbox = np.array(f['result'][0]['bbox']).tolist()
        if iou(gt_bbox, pred_bbox) >= 0.5:
            add = add_err(gt_pose, pred_pose, model_vertices)
            add *=1000 # changing unit
            add_errs.append(add)
            adds.append(add < diameter/10)
    mean_add_err = np.mean(add_errs)
    mean_add = np.mean(adds)
    print("Mean add accuracy of seq %d is: ", mean_add)
    embed()