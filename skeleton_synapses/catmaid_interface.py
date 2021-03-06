import json
from collections import defaultdict
import logging

from six import string_types

import networkx as nx
import numpy as np

from catpy.client import CatmaidClientApplication, make_url, CoordinateTransformer
from catpy.export import ExportWidget

from skeleton_synapses.dto import NodeInfo

NEUROCEAN_CONSTANTS = {
    'skel_id': 11524047,
    'project_id': 1,
    'image_stack_id': 1
}

DEV_CONSTANTS = {
    'skel_id': 18383770,
    'project_id': 4,
    'image_stack_id': 4
}

logger = logging.getLogger(__name__)
api_logger = logging.getLogger(__name__ + '.api')


def get_consecutive(lst):
    """
    Given an iterable of unique integers, return a list of lists where the elements of the inner lists are all
    consecutive and in ascending order.

    >>> get_consecutive([2, 4, 1, 5])
    [[1, 2], [4, 5]]
    """
    sorted_lst = sorted(lst)
    ret_lst = [[sorted_lst.pop(0)]]
    while sorted_lst:
        if sorted_lst[0] == ret_lst[-1][-1] + 1:
            ret_lst[-1].append(sorted_lst.pop(0))
        else:
            ret_lst.append([sorted_lst.pop(0)])

    return ret_lst


def extend_slices(broken_slices):
    """
    Given a dict whose keys are z-indexes of missing slices (as reported by stack_info), return a slice extension
    schema as required by ilastik's stack description json.

    Note: not sure what the value of the input dict should mean, so an assertion error is thrown if it is not 1.

    >>> extend_slices({'349': 1, '350': 1, '351': 1, '99': 1})
    [[98, [99]], [348, [349, 350]], [352, [351]]]
    """
    assert all(value == 1 for value in broken_slices.values()), 'Not sure what to do with broken_slice values != 1'
    d = defaultdict(list)
    for broken_block in get_consecutive(int(item) for item in broken_slices):
        pre, post = min(broken_block) - 1, max(broken_block) + 1
        idxs = list(broken_block)
        while idxs:
            d[pre].append(idxs.pop(0))
            if idxs:
                d[post].append(idxs.pop())

    return [[key, value] for key, value in sorted(d.items(), key=lambda x: x[0])]


def make_tile_url_template(image_base):
    """
    May not be correct for all bases
    """
    return make_url(image_base, "{z_index}/0/{y_index}_{x_index}.jpg")


def to_iterable(arg):
    if isinstance(arg, string_types):
        return [arg]
    try:
        return list(arg)
    except TypeError as e:
        if "is not iterable" in str(e):
            return [arg]
        raise e


def get_nodes_between(graph, root=None, leaves=None):
    """
    Find all nodes both downstream of the given root and upstream of any of the given leaves.

    Parameters
    ----------
    graph
    root
        If None, find the root of the tree
    leaves
        If None, find all leaves of the tree

    Returns
    -------

    """
    assert nx.is_directed_acyclic_graph(graph)

    if root is None:
        for node, degree in graph.in_degree_iter():
            if degree == 0:
                root = node
                break

    if leaves is None:
        leaves = [node for node, degree in graph.out_degree_iter() if degree == 0]

    output_set = set(leaves)
    for leaf in leaves:
        leaf_ancestors = nx.ancestors(graph, leaf)
        if root in leaf_ancestors:
            output_set.update(leaf_ancestors)

    return output_set - nx.ancestors(graph, root)


def get_subarbor_node_infos(tnid_parentid, coords_xyz, root=None, leaves=None):
    """


    Parameters
    ----------
    tnid_parentid : list of tuple
        List of pairs of (treenode_id, parent_id)
    coords_xyz : list of tuple
        List of (x, y, z) tuples
    root
        Most basal node to return
    leaves
        Most distal nodes to return

    Returns
    -------
    list of NodeInfo
    """
    node_infos = []
    if root is None and leaves is None:
        for (node_id, parent_id), (x, y, z) in zip(tnid_parentid, coords_xyz):
            node_infos.append(NodeInfo(int(node_id), x, y, z, None if parent_id is None else int(parent_id)))
        return node_infos

    g = nx.DiGraph()
    for (node_id, parent_id), (x, y, z) in zip(tnid_parentid, coords_xyz):
        node_id = int(node_id)
        g.add_node(node_id, node_info=NodeInfo(node_id, x, y, z, None if parent_id is None else int(parent_id)))
        if not parent_id:
            root = root or node_id
        else:
            g.add_edge(int(parent_id), node_id)

    for node_id in get_nodes_between(g, root, leaves):
        node_infos.append(g.node[node_id]['node_info'])

    return node_infos


class CatmaidSynapseSuggestionAPI(CatmaidClientApplication):
    def __init__(self, catmaid_client, stack_id_or_title=None):
        super(CatmaidSynapseSuggestionAPI, self).__init__(catmaid_client)
        self.export_widget = ExportWidget(catmaid_client)
        self.stack_id = self._get_stack_id(stack_id_or_title)

    def _get_stack_id(self, stack_id_or_title):
        try:
            return int(stack_id_or_title)
        except TypeError:
            if stack_id_or_title is None:
                return None
            stacks = self.get((self.project_id, 'stacks'))
            for stack in stacks:
                if stack['title'] == stack_id_or_title:
                    return stack['id']
            raise ValueError('Stack {} not found for project with ID {}'.format(repr(stack_id_or_title), self.project_id))

    def get_stack_description(self, stack_id_or_title, include_offset=True, cache_tiles=False):
        """
        Generate sufficient information for ilastik to read images from CATMAID.

        Parameters
        ----------
        stack_id_or_title : int or str
            Integer ID or string title of the image stack in CATMAID
        include_offset : bool, optional
            Whether to include the stack offset from the project. Including the offset makes it easier to align the
            skeleton geometry with the CATMAID images, but not including it makes it easier to align the ilastik and
            CATMAID images for debugging purposes. Defaults to True.
        cache_tiles : bool, optional
            Whether to cache the tiles (makes viewing them for debugging easier)

        Returns
        -------
        dict
            Information required by ilastik for getting images from CATMAID
        """
        stack_info = self.get_stack_info(stack_id_or_title)
        stack_mirror = stack_info['mirrors'][0]

        return {
            "_schema_name": "tiled-volume-description",
            "_schema_version": 1.0,

            "name": stack_info['stitle'],
            "format": stack_mirror['file_extension'],  # works for jpg
            "dtype": "uint8",  # not defined in stack_info
            "bounds_zyx": [stack_info['dimension'][axis] for axis in 'zyx'],

            # skeleton files do not necessarily use the same coordinates as the CATMAID viewer/tiles, there may be an
            # offset, encoded here. May not be correct, please check. Using this offset makes the ilastik and catmaid
            # z-coordinates not line up, but the skeleton file does.
            "view_origin_zyx": [
                -int(stack_info['translation'][axis]/stack_info['resolution'][axis]) * include_offset for axis in 'zyx'
                ],

            "resolution_zyx": [stack_info['resolution'][axis] for axis in 'zyx'],
            "tile_shape_2d_yx": [stack_mirror['tile_height'], stack_mirror['tile_width']],

            "tile_url_format": make_tile_url_template(stack_mirror['image_base']),  # may not be correct for all bases

            "output_axes": "xyz",  # DO NOT TOUCH

            "cache_tiles": cache_tiles,  # useful for debug viewing

            "extend_slices": extend_slices(stack_info['broken_slices'])
        }

    def get_project_title(self, stack_id_or_title):
        stack_info = self.get_stack_info(stack_id_or_title)

        return stack_info['ptitle']

    def _get_user_id(self, user_id_or_name):
        try:
            return int(user_id_or_name)
        except ValueError:
            users = self.get('user-list')
            for user in users:
                if user_id_or_name in [user['login'], user['full_name']]:
                    return user['id']
            raise ValueError('User {} not found.'.format(repr(user_id_or_name)))

    def get_connectors(self, user_id_or_name, date_from, date_to):
        """

        Parameters
        ----------
        user_id_or_name
        date_from : datetime.datetime
        date_to : datetime.datetime

        Returns
        -------
        dict
            Keys are connector IDs, values are coordinate dicts in project space
        """
        params = dict()

        if user_id_or_name:
            params['completed_by'] = self._get_user_id(user_id_or_name)
        if date_from:
            params['from'] = date_from.strftime('%Y%m%d')
        if date_to:
            params['to'] = date_to.strftime('%Y%m%d')

        output = dict()

        for row in self.get((self.project_id, 'connector/list/completed'), params):
            output[row[0]] = {dim: val for dim, val in zip('xyz', row[1])}

        return output

    def get_stack_info(self, stack_id_or_title):
        stack_id = self._get_stack_id(stack_id_or_title)
        return self.get((self.project_id, 'stack', stack_id, 'info'))

    def get_coord_transformer(self, stack_id_or_title=None):
        if stack_id_or_title is None:
            return CoordinateTransformer()
        else:
            stack_id = self._get_stack_id(stack_id_or_title)
            return CoordinateTransformer.from_catmaid(self._catmaid, stack_id)

    def get_transformed_treenode_and_connector_geometry(self, stack_id_or_title, *skeleton_ids):
        data = self.export_widget.get_treenode_and_connector_geometry(*skeleton_ids)
        if stack_id_or_title is None:
            return data

        transformer = self.get_coord_transformer(stack_id_or_title)

        for skid, skel_data in data['skeletons'].items():
            for treenode_id, treenode_data in skel_data['treenodes'].items():
                treenode_data['location'] = tuple(transformer.project_to_stack_array(treenode_data['location']))

            for connector_id, connector_data in skel_data['connectors'].items():
                connector_data['location'] = tuple(transformer.project_to_stack_array(connector_data['location']))

        return data

    def get_workflow_id(self, stack_id, detection_hash, tile_size=512, detection_notes=None):
        params = {'stack_id': stack_id, 'detection_hash': detection_hash, 'tile_size': tile_size}
        if detection_notes:
            params['algo_notes'] = detection_notes
        return self.get(('ext/synapsesuggestor/synapse-detection/workflow'), params)['workflow_id']

    def get_project_workflow_id(self, workflow_id, association_hash, association_notes=None):
        params = {'workflow_id': workflow_id, 'association_hash': association_hash}
        if association_notes:
            params['algo_notes'] = association_notes
        return self.get(
            ('ext/synapsesuggestor/treenode-association', self.project_id, 'workflow'), params
        )['project_workflow_id']

    def get_node_infos(self, skeleton_ids, stack_id_or_title=None, root=None, leaves=None):
        """
        Get locations of treenodes as xyz coordinates. If a stack id or title is given, transform the coordinates into
        stack coords.

        Parameters
        ----------
        skeleton_ids : int or str or iterable
        stack_id_or_title : int or str

        Returns
        -------
        tuple of (array-like, array-like)
            An M-length array of node IDs and an M*3 array of XYZ coordinates, for M nodes
        """
        transformer = self.get_coord_transformer(stack_id_or_title)
        coord_type = int if stack_id_or_title is not None else float
        skeleton_ids = set(to_iterable(skeleton_ids))

        node_infos = []
        for skeleton_id in skeleton_ids:
            treenodes = self.get((self.project_id, 'skeletons', skeleton_id, 'compact-detail'))[0]
            treenodes_arr = np.array(treenodes)
            coords = transformer.project_to_stack_array(treenodes_arr[:, 3:6].astype(float))
            node_infos.extend(get_subarbor_node_infos(treenodes_arr[:, :2], coords.astype(coord_type), root, leaves))

        return node_infos

    def get_detected_tiles(self, workflow_id):
        """
        xyz

        Parameters
        ----------
        workflow_id

        Returns
        -------
        set of tuple
            Tuples of tile indices in XYZ order
        """
        data = self.get('ext/synapsesuggestor/synapse-detection/tiles/detected', {'workflow_id': workflow_id})
        return {tuple(item) for item in data}

    def add_synapse_slices_to_tile(self, workflow_id, synapse_slices, tile_idx):
        """


        Parameters
        ----------
        workflow_id
        synapse_slices : dict
            Properties are id, wkt_str, size_px, xs_centroid, ys_centroid, uncertainty
        tile_idx : tuple
            Tile index in ZYX order

        Returns
        -------

        """
        data = {
            'workflow_id': workflow_id,
            'synapse_slices': [json.dumps(synapse_slice) for synapse_slice in synapse_slices],
            'z_idx': tile_idx[0],
            'y_idx': tile_idx[1],
            'x_idx': tile_idx[2]
        }

        url = 'ext/synapsesuggestor/synapse-detection/tiles/insert-synapse-slices'
        api_logger.debug('POST to {}\n{}'.format(url, data))
        response_data = self.post(url, data)
        api_logger.debug('Returned\n{}'.format(response_data))

        return {int(key): value for key, value in response_data.items()}

    def agglomerate_synapses(self, synapse_slice_ids):
        return self.post(
            'ext/synapsesuggestor/synapse-detection/slices/agglomerate',
            {'synapse_slices': list(synapse_slice_ids)}
        )

    def add_synapse_treenode_associations(self, associations, project_workflow_id=None):
        """

        Parameters
        ----------
        associations : list of tuples
            [(synapse_slice_id, treenode_id, contact_px), ...]
        project_workflow_id

        Returns
        -------

        """
        data = {'associations': [json.dumps(association) for association in associations]}
        if project_workflow_id is not None:
            data['project_workflow_id'] = project_workflow_id
        return self.post(('ext/synapsesuggestor/treenode-association', self.project_id, 'add'), data)

    def get_treenode_synapse_associations(self, skeleton_id, project_workflow_id=None):
        params = {'skid': skeleton_id}
        if project_workflow_id is not None:
            params['project_workflow_id'] = project_workflow_id

        return self.get(('ext/synapsesuggestor/treenode-association', self.project_id, 'get'), params)

    def get_synapse_bounds(self, synapse_ids, z_padding=1, xy_padding=10):
        params = {'synapse_object_ids': list(synapse_ids), 'z_padding': int(z_padding), 'xy_padding': int(xy_padding)}

        return self.get('ext/synapsesuggestor/analysis/synapse-extents', params)

    def sample_treenodes(self, count=None, seed=None):
        params = dict()
        if count:
            params['count'] = count
        if seed is not None:
            params['seed'] = seed

        return self.get(('ext/synapsesuggestor/training-data', self.project_id, 'treenodes/sample'), params)

    def treenodes_by_tag(self, *tags):
        return self.get(('ext/synapsesuggestor/training-data', self.project_id, 'treenodes/label'), {'tags': tags})

    def get_synapses_near_skeletons(self, skeleton_ids, project_workflow_id=None, distance=600):
        skids = to_iterable(skeleton_ids)

        output = []
        for skid in skids:
            params = {'skid': skid, 'distance': distance}
            if project_workflow_id is not None:
                params['project_workflow_id'] = project_workflow_id

            response = self.get(('ext/synapsesuggestor/treenode-association', self.project_id, 'get-distance'), params)
            output.extend(dict(zip(response['columns'], row)) for row in response['data'])

        return output

    def get_nodes_in_roi(self, roi_xyz, stack_id_or_title=None):
        """
        Get the nodes in the ROI with their coordinates relative to the top-left corner of the ROI.

        Parameters
        ----------
        roi_xyz : np.array
            [[xmin, ymin, zmin],[xmax, ymax, zmax]] in stack space
        stack_id_or_title

        Returns
        -------

        """
        transformer = self.get_coord_transformer(stack_id_or_title)
        # convert a half-closed [xyz, xyz) ROI for slice indexing into closed [xyz, xyz] for geometric intersection
        intersection_roi = roi_xyz - np.array([[0, 0, 0], [1, 1, 1]])
        roi_xyz_p = transformer.stack_to_project_array(intersection_roi)
        data = {
            'left': roi_xyz_p[0, 0],
            'top': roi_xyz_p[0, 1],
            'z1': roi_xyz_p[0, 2],
            'right': roi_xyz_p[1, 0],
            'bottom': roi_xyz_p[1, 1],
            'z2': roi_xyz_p[1, 2]
        }
        response = self.post((self.project_id, '/node/list'), data)
        treenodes = dict()
        for treenode_row in response[0]:
            tnid, _, x, y, z, _, _, skid, _, _ = treenode_row
            if not in_roi(roi_xyz_p, [x, y, z]):
                # API returns treenodes which are out of ROI if they have an edge which passes through the ROI
                continue

            treenodes[tnid] = {
                'coords': {
                    'x': int(transformer.project_to_stack_coord('x', x) - roi_xyz[0, 0]),
                    'y': int(transformer.project_to_stack_coord('y', y) - roi_xyz[0, 1]),
                    'z': int(transformer.project_to_stack_coord('z', z) - roi_xyz[0, 2])
                },
                'skeleton_id': skid,
                'treenode_id': tnid
            }

        return treenodes


def in_roi(roi_xyz, coords_xyz):
    """Closed interval: use intersection_roi"""
    x, y, z = coords_xyz
    return all([
        roi_xyz[0, 0] <= x <= roi_xyz[1, 0],
        roi_xyz[0, 1] <= y <= roi_xyz[1, 1],
        roi_xyz[0, 2] <= z <= roi_xyz[1, 2]
    ])
