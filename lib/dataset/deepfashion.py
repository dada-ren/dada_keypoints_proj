# ------------------------------------------------------------------------------
# Copyright (c) Microsoft
# Licensed under the MIT License.
# Written by Bin Xiao (Bin.Xiao@microsoft.com)
# ------------------------------------------------------------------------------

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import defaultdict
from collections import OrderedDict
import logging
import os

#from pycocotools.coco import COCO
from xtcocotools.coco import COCO
from xtcocotools.cocoeval import COCOeval
#from pycocotools.cocoeval import COCOeval
import json_tricks as json
import numpy as np

from dataset.JointsDataset import JointsDataset
from nms.nms import oks_nms
from nms.nms import soft_oks_nms


logger = logging.getLogger(__name__)


class CUSTOMCOCODataset(JointsDataset):
    '''
    deepfashion
    "gt_class_keypoints_dict":{
        1: (0, 8),
        }
    '''

    def __init__(self, cfg, root, image_set, is_train, transform=None):
        super().__init__(cfg, root, image_set, is_train, transform)
        self.nms_thre = cfg.TEST.NMS_THRE
        self.image_thre = cfg.TEST.IMAGE_THRE
        self.soft_nms = cfg.TEST.SOFT_NMS
        self.oks_thre = cfg.TEST.OKS_THRE
        self.in_vis_thre = cfg.TEST.IN_VIS_THRE
        self.bbox_file = cfg.TEST.COCO_BBOX_FILE
        self.use_gt_bbox = cfg.TEST.USE_GT_BBOX
        self.image_width = cfg.MODEL.IMAGE_SIZE[0]
        self.image_height = cfg.MODEL.IMAGE_SIZE[1]
        self.aspect_ratio = self.image_width * 1.0 / self.image_height
        self.pixel_std = 200

        print('anno:', self._get_ann_file_keypoint())
        self.coco = COCO(self._get_ann_file_keypoint())

        # deal with class names
        cats = [cat['name']
                for cat in self.coco.loadCats(self.coco.getCatIds())]
        self.classes = ['__background__'] + cats
        logger.info('=> classes: {}'.format(self.classes))
        self.num_classes = len(self.classes)
        self._class_to_ind = dict(zip(self.classes, range(self.num_classes)))
        self._class_to_coco_ind = dict(zip(cats, self.coco.getCatIds()))
        self._coco_ind_to_class_ind = dict(
            [
                (self._class_to_coco_ind[cls], self._class_to_ind[cls])
                for cls in self.classes[1:]
            ]
        )

        # load image file names
        self.image_set_index = self._load_image_set_index()
        self.num_images = len(self.image_set_index)
        logger.info('=> num_images: {}'.format(self.num_images))

        
        self.num_joints = 8
        self.flip_pairs = [[0, 1], [2, 3], [4, 5], [6, 7]] # （L,R）,从上至下    AIC_pose
        # self.parent_ids = None
        # self.upper_body_ids = (3, 0, 4, 1, 5, 2)
        # self.lower_body_ids = (9, 6, 10, 7, 11, 8)
        #
        # self.joints_weight = np.array(
        #     [
        #         1.5, 1.2, 1., 1., 1.2, 1.5, 1., 1., 1.5, 1.2, 1., 1., 1.2, 1.5
        #     ],
        #     dtype=np.float32
        # ).reshape((self.num_joints, 1))

        self.parent_ids = None
        self.upper_body_ids = (0, 1, 2, 3)
        self.lower_body_ids = (4, 5, 6, 7)

        self.joints_weight = np.ones((self.num_joints, 1), dtype=np.float32
                                    ).reshape((self.num_joints, 1))

        self.file_name = []
        for id in self.image_set_index:
            self.file_name.append(self.coco.imgs[id]['file_name'])

        self.db = self._get_db()

        if is_train and cfg.DATASET.SELECT_DATA:
            self.db = self.select_data(self.db)

        logger.info('=> load {} samples'.format(len(self.db)))

    '''
    def _get_ann_file_keypoint(self):
        """ self.root / annotations / person_keypoints_train2017.json """
        return os.path.join(
            self.root,
            self.image_set + '.json'
        )
    '''
    def _get_ann_file_keypoint(self):
        if 'train'  in self.image_set:
            directory = self.image_set
            #if self.mini_dataset:
            #    return os.path.join(self.root, directory, 'train-coco_style-32.json')
            #else:
            #    return os.path.join(self.root, directory, 'train-coco_style.json')
            return os.path.join(self.root, directory, 'train-coco_style.json')
        elif 'validation'  in self.image_set:
            directory = self.image_set
            #if self.mini_dataset:
            #    return os.path.join(self.root, directory, 'val-coco_style-64.json')
            #else:
            #    return os.path.join(self.root, directory, 'val-coco_style.json')
            return os.path.join(self.root, directory, 'val-coco_style.json')
        elif 'test'  in self.image_set:
            directory = 'json_for_test'
            return os.path.join(self.root, directory, 'keypoints_test_information.json')
        else:
            raise NotImplementedError

    def _load_image_set_index(self):
        """ image id: int """
        image_ids = self.coco.getImgIds()
        return image_ids

    def _get_db(self):
        if self.is_train or self.use_gt_bbox:
            # use ground truth bbox
            gt_db = self._load_coco_keypoint_annotations()
        else:
            # use bbox from detection
            gt_db = self._load_coco_person_detection_results()
        return gt_db

    def _load_coco_keypoint_annotations(self):
        """ ground truth bbox and keypoints """
        gt_db = []
        for index in self.image_set_index:
            gt_db.extend(self._load_coco_keypoint_annotation_kernal(index))
        return gt_db

    def _load_coco_keypoint_annotation_kernal(self, index):
        """
        coco ann: [u'segmentation', u'area', u'iscrowd', u'image_id', u'bbox', u'category_id', u'id']
        iscrowd:
            crowd instances are handled by marking their overlaps with all categories to -1
            and later excluded in training
        bbox:
            [x1, y1, w, h]
        :param index: coco image id
        :return: db entry
        """
        im_ann = self.coco.loadImgs(index)[0]
        # print('im_ann:', im_ann)
        width = im_ann['width']
        height = im_ann['height']

        annIds = self.coco.getAnnIds(imgIds=index)
        objs = self.coco.loadAnns(annIds)

        # sanitize bboxes
        valid_objs = []
        for obj in objs:
            x, y, w, h = obj['bbox']
            x1 = np.max((0, x))
            y1 = np.max((0, y))
            x2 = np.min((width - 1, x1 + np.max((0, w - 1))))
            y2 = np.min((height - 1, y1 + np.max((0, h - 1))))
            if obj["area"] > 0 and x2 >= x1 and y2 >= y1:
                obj['clean_bbox'] = [x1, y1, x2-x1, y2-y1]
                valid_objs.append(obj)
        objs = valid_objs

        rec = []
        for obj in objs:
            cls = self._coco_ind_to_class_ind[obj['category_id']]
            #if cls != 1:
            #    continue

            # ignore objs without keypoints annotation
            #if max(obj['keypoints']) == 0:
            #    continue

            joints_3d = np.zeros((self.num_joints, 3), dtype=np.float)
            joints_3d_vis = np.zeros((self.num_joints, 3), dtype=np.float)
            for ipt in range(self.num_joints):
                joints_3d[ipt, 0] = obj['keypoints'][ipt * 3 + 0]
                joints_3d[ipt, 1] = obj['keypoints'][ipt * 3 + 1]
                joints_3d[ipt, 2] = 0
                t_vis = obj['keypoints'][ipt * 3 + 2]
                if t_vis > 1:
                    t_vis = 1
                joints_3d_vis[ipt, 0] = t_vis
                joints_3d_vis[ipt, 1] = t_vis
                joints_3d_vis[ipt, 2] = 0

            center, scale = self._box2cs(obj['clean_bbox'][:4])
            #center, scale = self._box2cs(obj['bbox'][:4])
            rec.append({
                'image': self.image_path_from_index(index),
                'center': center,
                'scale': scale,
                'joints_3d': joints_3d,
                'joints_3d_vis': joints_3d_vis,
                'filename': '',
                'imgnum': 0,
                'category_id':obj['category_id'],
                'area': obj['area']
            })

        return rec

    def _box2cs(self, box):
        x, y, w, h = box[:4]
        return self._xywh2cs(x, y, w, h)

    def _xywh2cs(self, x, y, w, h):
        center = np.zeros((2), dtype=np.float32)
        center[0] = x + w * 0.5
        center[1] = y + h * 0.5

        if w > self.aspect_ratio * h:
            h = w * 1.0 / self.aspect_ratio
        elif w < self.aspect_ratio * h:
            w = h * self.aspect_ratio
        scale = np.array(
            [w * 1.0 / self.pixel_std, h * 1.0 / self.pixel_std],
            dtype=np.float32)
        if center[0] != -1:
            scale = scale * 1.25

        return center, scale

    def image_path_from_index(self, index):
        """ example: images / train2017 / 000000119993.jpg """
        #file_name = '%012d.jpg' % index
        # if '2014' in self.image_set:
        #     file_name = 'COCO_%s_' % self.image_set + file_name

        # prefix = 'test2017' if 'test' in self.image_set else self.image_set
        # data_name = prefix + '.zip@' if self.data_format == 'zip' else prefix
        
        #data_name = self.image_set.split('_')[-1]
        im_ann = self.coco.loadImgs(index)[0]
        file_name = im_ann['file_name']
        # file_name = self.file_name[index]
        #image_path = os.path.join(self.root , data_name, file_name)
        image_path = os.path.join(self.root , file_name)
       # print('image_path:', image_path)

        return image_path

    def _load_coco_person_detection_results(self):
        all_boxes = None
        with open(self.bbox_file, 'r') as f:
            all_boxes = json.load(f)

        if not all_boxes:
            logger.error('=> Load %s fail!' % self.bbox_file)
            return None

        logger.info('=> Total boxes: {}'.format(len(all_boxes)))

        kpt_db = []
        num_boxes = 0
        for n_img in range(0, len(all_boxes)):
            det_res = all_boxes[n_img]
            if det_res['category_id'] != 1:
                continue
            img_name = self.image_path_from_index(det_res['image_id'])

            box = det_res['bbox']
            score = det_res['score']

            if score < self.image_thre:
                continue

            num_boxes = num_boxes + 1

            center, scale = self._box2cs(box)
            joints_3d = np.zeros((self.num_joints, 3), dtype=np.float)
            joints_3d_vis = np.ones(
                (self.num_joints, 3), dtype=np.float)
            kpt_db.append({
                'image': img_name,
                'center': center,
                'scale': scale,
                'score': score,
                'joints_3d': joints_3d,
                'joints_3d_vis': joints_3d_vis,
            })

        logger.info('=> Total boxes after fliter low score@{}: {}'.format(
            self.image_thre, num_boxes))
        return kpt_db

    @staticmethod
    def _get_mapping_id_name(imgs):
        """
        Args:
            imgs (dict): dict of image info.

        Returns:
            tuple: Image name & id mapping dicts.

            - id2name (dict): Mapping image id to name.
            - name2id (dict): Mapping image name to id.
        """
        id2name = {}
        name2id = {}
        for image_id, image in imgs.items():
            file_name = image['file_name']
            id2name[image_id] = file_name
            name2id[file_name] = image_id

        return id2name, name2id

    def evaluate(self, cfg, preds, output_dir, all_boxes, img_path,
                 *args, **kwargs):
        rank = cfg.RANK

        res_folder = os.path.join(output_dir, 'results')
        if not os.path.exists(res_folder):
            try:
                os.makedirs(res_folder)
            except Exception:
                logger.error('Fail to make {}'.format(res_folder))

        res_file = os.path.join(
            res_folder, 'keypoints_{}_results_{}.json'.format(
                self.image_set, rank)
        )

        # person x (keypoints)
        self.id2name, self.name2id = self._get_mapping_id_name(
            self.coco.imgs)

        #print(self.name2id)
        
        _kpts = []
        for idx, kpt in enumerate(preds):
            _kpts.append({
                'keypoints': kpt,
                'center': all_boxes[idx][0:2],
                'scale': all_boxes[idx][2:4],
                'area': all_boxes[idx][4],
                'score': all_boxes[idx][5],
                # 'image': int(img_path[idx][-16:-4]) # for coco
                'image':self.name2id[img_path[idx][len(self.root)+1:]]
            })
        # image x person x (keypoints)
        kpts = defaultdict(list)
        for kpt in _kpts:
            kpts[kpt['image']].append(kpt)

        # rescoring and oks nms
        num_joints = self.num_joints
        in_vis_thre = self.in_vis_thre
        oks_thre = self.oks_thre
        oks_nmsed_kpts = []
        for img in kpts.keys():
            img_kpts = kpts[img]
            for n_p in img_kpts:
                box_score = n_p['score']
                kpt_score = 0
                valid_num = 0
                for n_jt in range(0, num_joints):
                    t_s = n_p['keypoints'][n_jt][2]
                    if t_s > in_vis_thre:
                        kpt_score = kpt_score + t_s
                        valid_num = valid_num + 1
                if valid_num != 0:
                    kpt_score = kpt_score / valid_num
                # rescoring
                n_p['score'] = kpt_score * box_score

            if self.soft_nms:
                keep = soft_oks_nms(
                    [img_kpts[i] for i in range(len(img_kpts))],
                    oks_thre
                )
            else:
                keep = oks_nms(
                    [img_kpts[i] for i in range(len(img_kpts))],
                    oks_thre
                )

            if len(keep) == 0:
                oks_nmsed_kpts.append(img_kpts)
            else:
                oks_nmsed_kpts.append([img_kpts[_keep] for _keep in keep])

        self._write_coco_keypoint_results(
            oks_nmsed_kpts, res_file)
        if 'test' not in self.image_set:
            info_str = self._do_python_keypoint_eval(
                res_file, res_folder)
            name_value = OrderedDict(info_str)
            return name_value, name_value['AP']
        else:
            return {'Null': 0}, 0

    def _write_coco_keypoint_results(self, keypoints, res_file):
        data_pack = [
            {
                'cat_id': self._class_to_coco_ind[cls],
                'cls_ind': cls_ind,
                'cls': cls,
                'ann_type': 'keypoints',
                'keypoints': keypoints
            }
            for cls_ind, cls in enumerate(self.classes) if not cls == '__background__'
        ]

        results = self._coco_keypoint_results_one_category_kernel(data_pack[0])
        logger.info('=> writing results json to %s' % res_file)
        with open(res_file, 'w') as f:
            json.dump(results, f, sort_keys=True, indent=4)
        try:
            json.load(open(res_file))
        except Exception:
            content = []
            with open(res_file, 'r') as f:
                for line in f:
                    content.append(line)
            content[-1] = ']'
            with open(res_file, 'w') as f:
                for c in content:
                    f.write(c)

    def _coco_keypoint_results_one_category_kernel(self, data_pack):
        cat_id = data_pack['cat_id']
        keypoints = data_pack['keypoints']
        cat_results = []

        for img_kpts in keypoints:
            if len(img_kpts) == 0:
                continue

            _key_points = np.array([img_kpts[k]['keypoints']
                                    for k in range(len(img_kpts))])
            key_points = np.zeros(
                (_key_points.shape[0], self.num_joints * 3), dtype=np.float
            )

            for ipt in range(self.num_joints):
                key_points[:, ipt * 3 + 0] = _key_points[:, ipt, 0]
                key_points[:, ipt * 3 + 1] = _key_points[:, ipt, 1]
                key_points[:, ipt * 3 + 2] = _key_points[:, ipt, 2]  # keypoints score.

            result = [
                {
                    'image_id': img_kpts[k]['image'],
                    'category_id': cat_id,
                    'keypoints': list(key_points[k]),
                    'score': img_kpts[k]['score'],
                    'center': list(img_kpts[k]['center']),
                    'scale': list(img_kpts[k]['scale'])
                }
                for k in range(len(img_kpts))
            ]
            cat_results.extend(result)

        return cat_results

    # def _write_coco_keypoint_results(self, keypoints, res_file):
    #     """Write results into a json file."""
    #     data_pack = [{
    #         'cat_id': self._class_to_coco_ind[cls],
    #         'cls_ind': cls_ind,
    #         'cls': cls,
    #         'ann_type': 'keypoints',
    #         'keypoints': keypoints
    #     } for cls_ind, cls in enumerate(self.classes)
    #                  if not cls == '__background__']

    #     results = self._coco_keypoint_results_one_category_kernel(data_pack[0])

    #     with open(res_file, 'w') as f:
    #         json.dump(results, f, sort_keys=True, indent=4)

    
    

    # def _coco_keypoint_results_one_category_kernel(self, data_pack):
    #     """Get coco keypoint results."""
    #     cat_id = data_pack['cat_id']
    #     keypoints = data_pack['keypoints']
    #     cat_results = []

    #     for img_kpts in keypoints:
    #         if len(img_kpts) == 0:
    #             continue

    #         _key_points = np.array(
    #             [img_kpt['keypoints'] for img_kpt in img_kpts])
    #         key_points = _key_points.reshape(-1,
    #                                          14 * 3)

    #         result = [{
    #             'image_id': img_kpt['image_id'],
    #             'category_id': cat_id,
    #             'keypoints': key_point.tolist(),
    #             'score': float(img_kpt['score']),
    #             'center': img_kpt['center'].tolist(),
    #             'scale': img_kpt['scale'].tolist()
    #         } for img_kpt, key_point in zip(img_kpts, key_points)]

    #         cat_results.extend(result)

    #     return cat_results

    def _do_python_keypoint_eval(self, res_file, res_folder):
        print('res_file:', res_file)
        coco_dt = self.coco.loadRes(res_file)
        # print('coco_dt',coco_dt)
        self.sigmas = np.array([
        0.026, 0.026, 0.025, 0.025, 0.035, 0.035, 0.079, 0.079
    ])
        coco_eval = COCOeval(self.coco, coco_dt, 'keypoints', self.sigmas, use_area=False)
        coco_eval.params.useSegm = None
        coco_eval.evaluate()
        coco_eval.accumulate()
        coco_eval.summarize()

        stats_names = ['AP', 'Ap .5', 'AP .75', 'AP (M)', 'AP (L)', 'AR', 'AR .5', 'AR .75', 'AR (M)', 'AR (L)']

        info_str = []
        for ind, name in enumerate(stats_names):
            info_str.append((name, coco_eval.stats[ind]))

        return info_str

    def get_channel_index(self, class_id):
        gt_class_keypoints_dict =  {1: (0, 8),2:(0,8),3:(0,8)}

        if isinstance(class_id, int):
            return list(range(gt_class_keypoints_dict[class_id]))
        elif isinstance(class_id, list):
            return [list(range(gt_class_keypoints_dict[i])) for i in class_id]

    def generate_target(self, joints, joints_vis):
        '''
        :param joints:  [num_joints, 3]
        :param joints_vis: [num_joints, 3]
        :return: target, target_weight(1: visible, 0: invisible)
        '''
        # target_weight = np.ones((self.num_joints, 1), dtype=np.float32)
        target_weight = np.zeros((self.num_joints, 1), dtype=np.float32)
        target_weight[:, 0] = joints_vis[:, 0]
        feat_stride = self.image_size / self.heatmap_size

        if self.target_type == 'gaussian':
            target = np.zeros((self.num_joints,
                               self.heatmap_size[1],
                               self.heatmap_size[0]),
                              dtype=np.float32)

            tmp_size = self.sigma * 3

            for joint_id in range(self.num_joints):
                mu_x = int(joints[joint_id][0] / feat_stride[0] + 0.5)
                mu_y = int(joints[joint_id][1] / feat_stride[1] + 0.5)
                # Check that any part of the gaussian is in-bounds
                ul = [int(mu_x - tmp_size), int(mu_y - tmp_size)]
                br = [int(mu_x + tmp_size + 1), int(mu_y + tmp_size + 1)]
                if ul[0] >= self.heatmap_size[0] or ul[1] >= self.heatmap_size[1] \
                        or br[0] < 0 or br[1] < 0:
                    # If not, just return the image as is
                    target_weight[joint_id] = 0
                    continue

                # # Generate gaussian
                size = 2 * tmp_size + 1
                x = np.arange(0, size, 1, np.float32)
                y = x[:, np.newaxis]
                                                                                                                
                x0 = y0 = size // 2
                # The gaussian is not normalized, we want the center value to equal 1
                g = np.exp(- ((x - x0) ** 2 + (y - y0) ** 2) / (2 * self.sigma ** 2))

                # Usable gaussian range
                g_x = max(0, -ul[0]), min(br[0], self.heatmap_size[0]) - ul[0]
                g_y = max(0, -ul[1]), min(br[1], self.heatmap_size[1]) - ul[1]
                # Image range
                img_x = max(0, ul[0]), min(br[0], self.heatmap_size[0])
                img_y = max(0, ul[1]), min(br[1], self.heatmap_size[1])

                v = target_weight[joint_id]
                if v > 0.5:
                    target[joint_id][img_y[0]:img_y[1], img_x[0]:img_x[1]] = \
                        g[g_y[0]:g_y[1], g_x[0]:g_x[1]]
        elif self.target_type == 'coordinate':
            target = joints[:, 0:2]
            target /= feat_stride
        else:
            raise NotImplementedError('Only support gaussian map and coordinate now!')

        if self.use_different_joints_weight:
            target_weight = np.multiply(target_weight, self.joints_weight)

        return target, target_weight
