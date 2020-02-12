#!/usr/bin/env python3

"""
    ##### BEGIN GPL LICENSE BLOCK #####
  This program is free software; you can redistribute it and/or
  modify it under the terms of the GNU General Public License
  as published by the Free Software Foundation; either version 2
  of the License, or (at your option) any later version.
  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.
  You should have received a copy of the GNU General Public License
  along with this program; if not, write to the Free Software Foundation,
  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
 ##### END GPL LICENSE BLOCK #####
"""
from __future__ import annotations  # Enables python 4.0 annotation typehints fx. class self-referencing
from typing import Union
import sys
import os
import shutil
import math
import mathutils

# Old exporter used cElementTree for speed, but it was deprecated to compatibility status in python 3.3
import xml.etree.ElementTree as ET  # Technically not following pep8, but this is the naming suggestion from the module
import bpy

from bpy_extras.io_utils import (
    axis_conversion
)

from . import i3d_properties


# Exporter is a singleton
class Exporter:

    def __init__(self, filepath: str, axis_forward, axis_up):
        self._scene_graph = SceneGraph()
        self._export_only_selection = False
        self._filepath = filepath
        self._file_indexes = {}
        self.shape_material_indexes = {}
        self.ids = {
            'shape': 1,
            'material': 1,
            'file': 1
        }

        self._global_matrix = axis_conversion(
            to_forward=axis_forward,
            to_up=axis_up,
        ).to_4x4()

        # Evaluate the dependency graph to make sure that all data is evaluated. As long as nothing changes, this
        # should only be 'heavy' to call the first time a mesh is exported.
        # https://docs.blender.org/api/current/bpy.types.Depsgraph.html
        self._depsgraph = bpy.context.evaluated_depsgraph_get()

        self._xml_build_skeleton_structure()
        self._xml_build_scene_graph()
        self._xml_parse_scene_graph()

        self._xml_export_to_file()

    def _xml_build_scene_graph(self):

        objects_to_export = bpy.context.scene.i3dio.object_types_to_export

        def new_graph_node(blender_object: Union[bpy.types.Object, bpy.types.Collection],
                           parent: SceneGraph.Node,
                           unpack_collection: bool = False):

            if not isinstance(blender_object, bpy.types.Collection):
                if blender_object.type not in objects_to_export:
                    return

            node = None
            if unpack_collection:
                node = parent
            else:
                node = self._scene_graph.add_node(blender_object, parent)
                print(f"Added Node with ID {node.id} and name {node.blender_object.name!r}")

            # Expand collection tree into the collection instance
            if isinstance(blender_object, bpy.types.Object):
                if blender_object.type == 'EMPTY':
                    if blender_object.instance_collection is not None:
                        # print(f'This is a collection instance')
                        new_graph_node(blender_object.instance_collection, node, unpack_collection=True)

            # Gets child objects/collections
            if isinstance(blender_object, bpy.types.Object):
                # print(f'Children of object')
                for child in blender_object.children:
                    new_graph_node(child, node)

            # Gets child objects if it is a collection
            if isinstance(blender_object, bpy.types.Collection):
                # print(f'Children collections')
                for child in blender_object.children:
                    new_graph_node(child, node)

                # print(f'Children objects in collection')
                for child in blender_object.objects:
                    if child.parent is None:
                        new_graph_node(child, node)

        selection = bpy.context.scene.i3dio.selection
        if selection == 'ALL':
            selection = bpy.context.scene.collection
            new_graph_node(selection, self._scene_graph.nodes[0])
        elif selection == 'ACTIVE_COLLECTION':
            selection = bpy.context.view_layer.active_layer_collection.collection
            new_graph_node(selection, self._scene_graph.nodes[0])
        elif selection == 'SELECTED_OBJECTS':
            # Generate active object list and loop over that somehow
            pass

        # for obj in bpy.context.selected_objects:
        #    # Objects directly in the scene only has the 'Master Collection' in the list,
        #    # which disappears once the object is added to any other collection
        #    if bpy.context.scene.collection in obj.users_collection and obj.parent is None:
        #       print(f"{obj.name!r} is at scene root")
        #       self.new_graph_node(obj, self._scene_graph.nodes[0])

    def _xml_build_skeleton_structure(self) -> None:
        """Builds the i3d file conforming to the standard specified at
        https://gdn.giants-software.com/documentation_i3d.php
        """
        self._tree = ET.Element('i3D')  # Create top level element
        self._tree.set('name', bpy.path.display_name_from_filepath(self._filepath))  # Name attribute

        # Xml scheme attributes as required by the i3d standard, even though most of the links are dead.
        self._tree.set('version', "1.6")
        self._tree.set('xmlns:xsi', "http://www.w3.org/2001/XMLSchema-instance")
        self._tree.set('xsi:noNamespaceSchemaLocation', "http://i3d.giants.ch/schema/i3d-1.6.xsd")

        # Asset export: Currently just a notice of which tool was used for generating the file
        element = ET.SubElement(self._tree, 'Asset')
        element = ET.SubElement(element, 'Export')
        element.set('program', 'Blender Exporter (Community)')
        element.set('version', sys.modules['i3dio'].bl_info.get('version'))  # Fetch version directly from bl_info

        # File export: References to external files such as images for materials (diffuse, normals etc.)
        ET.SubElement(self._tree, 'Files')

        # Material export: List of all materials used in the project
        ET.SubElement(self._tree, 'Materials')

        # Shapes export: All the shape data in the form of vertices and triangles. This section takes up a lot of space
        # and it would be preferable to export to an external shapes file (Giants Engine can do it by a binary save)
        ET.SubElement(self._tree, 'Shapes')

        # Dynamics export: Particle systems
        ET.SubElement(self._tree, 'Dynamics')

        # Scenegraph export: The entire scenegraph structure, with references to light, cameras, transforms and shapes
        ET.SubElement(self._tree, 'Scene')

        # Animation export: Animation sets with keyframes
        ET.SubElement(self._tree, 'Animation')

        # User attributes export: User generated attributes that might be used in scripts etc.
        ET.SubElement(self._tree, 'UserAttributes')

    def _xml_parse_scene_graph(self):

        def parse_node(node: SceneGraph.Node, node_element: ET.Element):

            self._xml_scene_object_general_data(node, node_element)

            if isinstance(node.blender_object, bpy.types.Collection):
                self._xml_scene_object_transform_group(node, node_element)
            else:
                node_type = node.blender_object.type
                if node_type == 'MESH':
                    self._xml_scene_object_shape(node, node_element)
                elif node_type == 'EMPTY':
                    self._xml_scene_object_transform_group(node, node_element)
                elif node_type == 'LIGHT':
                    self._xml_scene_object_light(node, node_element)
                elif node_type == 'CAMERA':
                    self._xml_scene_object_camera(node, node_element)
                if hasattr(node.blender_object.data, 'i3d_attributes'):
                    self._xml_object_properties(node.blender_object.data.i3d_attributes, node_element)

            for child in node.children.values():

                child_element = ET.SubElement(node_element,
                                              Exporter.blender_to_i3d(child.blender_object))
                parse_node(child, child_element)

        for root_child in self._scene_graph.nodes[0].children.values():
            root_child_element = ET.SubElement(self._tree.find('Scene'),
                                               Exporter.blender_to_i3d(root_child.blender_object))
            parse_node(root_child, root_child_element)

    def _xml_scene_object_general_data(self, node: SceneGraph.Node, node_element: ET.Element):
        self._xml_write_string(node_element, 'name', node.blender_object.name)
        self._xml_write_int(node_element, 'nodeId', node.id)
        if isinstance(node.blender_object, bpy.types.Collection):
            # Collections dont have any physical properties, but the transformgroups in i3d has so it is set to 0
            # in relation to it's parent so it stays purely organisational
            self._xml_write_string(node_element, 'translation', "0 0 0")
            self._xml_write_string(node_element, 'rotation', "0 0 0")
            self._xml_write_string(node_element, 'scale', "1 1 1")
        else:
            # Apply the space transformations depending on object, since lights and cameras has their z-axis reversed
            # in GE
            # If you want an explanation for A * B * A^-1 then go look up Transformation Matrices cause I can't
            # remember the specifics

            if node.blender_object == 'LIGHT' or node.blender_object.type == 'CAMERA':
                matrix = self._global_matrix @ node.blender_object.matrix_local
            else:
                matrix = self._global_matrix @ node.blender_object.matrix_local @ self._global_matrix.inverted()

            if node.blender_object.parent is not None:
                if node.blender_object.parent.type == 'CAMERA' or node.blender_object.parent.type == 'LIGHT':
                    matrix = self._global_matrix.inverted() @ matrix

            self._xml_write_string(node_element,
                                   'translation',
                                   "{0:.6f} {1:.6f} {2:.6f}".format(
                                       *[x * bpy.context.scene.unit_settings.scale_length
                                         for x in matrix.to_translation()]))

            self._xml_write_string(node_element,
                                   'rotation',
                                   "{0:.3f} {1:.3f} {2:.3f}".format(*[math.degrees(axis)
                                                                      for axis in matrix.to_euler('XYZ')]))

            self._xml_write_string(node_element,
                                   'scale',
                                   "{0:.6f} {1:.6f} {2:.6f}".format(*matrix.to_scale()))

            # Write the object transform properties from the blender UI into the object
            self._xml_object_properties(node.blender_object.i3d_attributes, node_element)

    def _xml_object_properties(self, propertygroup, element):
        for key in propertygroup.__annotations__.keys():
            prop = getattr(propertygroup, key)

            # Check if property is default somehow
            name = prop.name_i3d
            val = prop.value_i3d

            print(f"Name: {name}, Value: {val}, Type: {type(val)}, IsInstance: {isinstance(val, bool)}")

            if name != 'disabled' and val != i3d_properties.defaults[name]:
                print("Exporting property")
                if isinstance(val, float):
                    print("float")
                    self._xml_write_float(element, prop.name_i3d, val)
                elif isinstance(val, bool):  # Order matters, since bool is an int subclass!
                    print("bool")
                    self._xml_write_bool(element, prop.name_i3d, val)
                elif isinstance(val, int):
                    print("int")
                    self._xml_write_int(element, prop.name_i3d, val)
                elif isinstance(val, str):
                    print("string")
                    self._xml_write_string(element, prop.name_i3d, val)

    def _xml_add_material(self, material):
        materials_root = self._tree.find('Materials')
        material_element = materials_root.find(f".Material[@name={material.name!r}]")
        if material_element is None:
            material_element = ET.SubElement(materials_root, 'Material')
            self._xml_write_string(material_element, 'name', material.name)
            self._xml_write_int(material_element, 'materialId', self.ids['material'])

            if material.use_nodes:
                material_node = material.node_tree.nodes.get('Principled BSDF')
                if material_node is not None:
                    # Diffuse
                    material_node_color_socket = material_node.inputs['Base Color']
                    diffuse = material_node_color_socket.default_value
                    if material_node_color_socket.is_linked:
                        material_node_color_connected_node = material_node_color_socket.links[0].from_node
                        if material_node_color_connected_node.bl_idname == 'ShaderNodeRGB':
                            diffuse = material_node_color_connected_node.outputs[0].default_value

                        elif material_node_color_connected_node.bl_idname == 'ShaderNodeTexImage':
                            if material_node_color_connected_node.image is not None:
                                file_id = self._xml_add_file(material_node_color_connected_node.image.filepath)
                                texture_element = ET.SubElement(material_element, 'Texture')
                                self._xml_write_string(texture_element, 'fileId', f'{file_id:d}')

                        else:
                            print(f"Unsupported input {material_node_color_connected_node.bl_idname!r} "
                                  f"on 'Color' socket in 'Principled BSDF' of material {material.name!r}")

                    self._xml_write_string(material_element,
                                           'diffuseColor',
                                           "{0:.6f} {1:.6f} {2:.6f} {3:.6f}".format(
                                               *diffuse))

                    # Specular
                    self._xml_write_string(material_element,
                                           'specularColor',
                                           f"{material_node.inputs['Roughness'].default_value:f} "
                                           f"{material_node.inputs['Specular'].default_value:.6f} "
                                           f"{material_node.inputs['Metallic'].default_value:f}")

                    # Normal
                    normal_node_socket = material_node.inputs['Normal']
                    if normal_node_socket.is_linked:
                        normal_map_node = normal_node_socket.links[0].from_node
                        if normal_map_node.bl_idname == 'ShaderNodeNormalMap':
                            normal_map_color_socket = normal_map_node.inputs['Color']
                            if normal_map_color_socket.is_linked:
                                texture_node = normal_map_color_socket.links[0].from_node
                                if texture_node.bl_idname == 'ShaderNodeTexImage':
                                    if texture_node.image is not None:
                                        file_id = self._xml_add_file(texture_node.image.filepath)
                                        normal_element = ET.SubElement(material_element, 'Normalmap')
                                        self._xml_write_string(normal_element, 'fileId', f'{file_id:d}')
                                else:
                                    print(f"Unknown color input of type: {normal_map_node.bl_idname!r} for normal map")
                        else:
                            print(f"Unknown normal input of type: {normal_map_node.bl_idname!r} for bdsf")

                separate_rgb_node = material.node_tree.nodes.get('Separate RGB')
                if separate_rgb_node is not None:
                    image_socket = separate_rgb_node.inputs['Image']
                    if image_socket.is_linked:
                        gloss_image_node = image_socket.links[0].from_node
                        if gloss_image_node.bl_idname == 'ShaderNodeTexImage':
                            if gloss_image_node.image is not None:
                                file_id = self._xml_add_file(gloss_image_node.image.filepath)
                                normal_element = ET.SubElement(material_element, 'Glossmap')
                                self._xml_write_string(normal_element, 'fileId', f'{file_id:d}')
                        else:
                            print(f"Unknown image input of type: {gloss_image_node.bl_idname!r} for Separate RGB")

            else:
                self._xml_write_string(material_element,
                                       'diffuseColor',
                                       "{0:.6f} {1:.6f} {2:.6f} {3:.6f}".format(*material.diffuse_color))

                self._xml_write_string(material_element,
                                       'specularColor',
                                       f"{material.roughness:f} {1:.6f} {material.metallic:f}")

            self.ids['material'] += 1

        return int(material_element.get('materialId'))

    def _xml_add_file(self, filepath, file_folder='textures') -> int:
        print("Relative path: " + filepath)
        filepath_absolute = bpy.path.abspath(filepath)
        print("Absolute path: " + filepath_absolute)
        print("Path sep: " + bpy.path.native_pathsep(filepath))
        files_root = self._tree.find('Files')
        filename = filepath_absolute[filepath_absolute.rfind('\\') + 1:len(filepath_absolute)]
        filepath_resolved = filepath_absolute
        filepath_i3d = self._filepath[0:self._filepath.rfind('\\') + 1]
        file_structure = bpy.context.scene.i3dio.file_structure

        relative_filter = 'data\shared'
        try:
            filepath_resolved = filepath_absolute.replace(filepath_absolute[0:filepath_absolute.index(relative_filter)], '$')
        except ValueError:
            pass

        # Resolve the filename and write the file
        if filepath_resolved[0] != '$' and bpy.context.scene.i3dio.copy_files:
            output_dir = ""
            if file_structure == 'FLAT':
                pass  # Default settings, kept for clarity when viewing code
            elif file_structure == 'MODHUB':
                output_dir = file_folder + '\\'
            elif file_structure == 'BLENDER':
                if filepath.count("..\\") <= 3:  # Limits the distance a file can be from the blend file to three
                    # relative steps to avoid copying entire folder structures ny mistake. Defaults to a absolute path.
                    output_dir = filepath[2:filepath.rfind('\\') + 1]  # Remove blender relative notation and filename
                else:
                    output_dir = filepath_absolute[0:filepath_absolute.rfind('\\') + 1]

            filepath_resolved = output_dir + filename
            # print("Filepath org:" + filepath)
            # print("Filename: " + filename)
            # print("Filepath i3d: " + filepath_i3d)
            # print("Out Dir: " + output_dir)

            if filepath_resolved != filepath_absolute and filepath_resolved not in self._file_indexes:
                if bpy.context.scene.i3dio.overwrite_files or not os.path.exists(filepath_i3d + output_dir + filename):
                    print("Path: " + filepath_i3d + output_dir)
                    os.makedirs(filepath_i3d + output_dir, exist_ok=True)
                    try:
                        shutil.copy(filepath_absolute, filepath_i3d + output_dir)
                    except shutil.SameFileError:
                        pass  # Ignore writing file if it already exist

        # Predicate search does NOT play nicely with the filepath names, so we loop the old fashioned way
        if filepath_resolved in self._file_indexes:
            return self._file_indexes[filepath_resolved]
        else:
            file_element = ET.SubElement(files_root, 'File')
            file_id = self.ids['file']
            self.ids['file'] += 1
            self._file_indexes[filepath_resolved] = file_id

            self._xml_write_int(file_element, 'fileId', file_id)
            self._xml_write_string(file_element, 'filename', filepath_resolved)
            return file_id

    def _xml_scene_object_shape(self, node: SceneGraph.Node, node_element: ET.Element):

        ###############################################
        # Mesh export section
        ###############################################
        shape_root = self._tree.find('Shapes')

        # Check if the mesh has already been defined in the i3d file
        indexed_triangle_element = shape_root.find(f".IndexedTriangleSet[@name={node.blender_object.data.name!r}]")
        if indexed_triangle_element is None:
            shape_id = self.ids['shape']
            self.ids['shape'] += 1

            indexed_triangle_element = ET.SubElement(shape_root, 'IndexedTriangleSet')

            self._xml_write_string(indexed_triangle_element, 'name', node.blender_object.data.name)
            self._xml_write_int(indexed_triangle_element, 'shapeId', shape_id)

            if bpy.context.scene.i3dio.apply_modifiers:
                # Generate an object evaluated from the dependency graph
                # The copy is important since the depsgraph will store changes to the evaluated object
                obj = node.blender_object.evaluated_get(self._depsgraph).copy()
            else:
                obj = node.blender_object.copy()

            # Generates a new mesh to not interfere with the existing one.
            mesh = obj.to_mesh(preserve_all_data_layers=True, depsgraph=self._depsgraph)

            conversion_matrix = self._global_matrix
            if bpy.context.scene.i3dio.apply_unit_scale:
                conversion_matrix = mathutils.Matrix.Scale(bpy.context.scene.unit_settings.scale_length, 4) \
                                    @ conversion_matrix

            mesh.transform(conversion_matrix)
            if conversion_matrix.is_negative:
                mesh.flip_normals()

            # Calculates triangles from mesh polygons
            mesh.calc_loop_triangles()
            # Recalculates normals after the scaling has messed with them
            mesh.calc_normals_split()

            vertices_element = ET.SubElement(indexed_triangle_element, 'Vertices')
            triangles_element = ET.SubElement(indexed_triangle_element, 'Triangles')
            subsets_element = ET.SubElement(indexed_triangle_element, 'Subsets')

            self._xml_write_int(triangles_element, 'count', len(mesh.loop_triangles))

            # Create and assign default material if it does not exist already. This material will persist in the blender
            # file so you can change the default look if you want to through the blender interface
            if len(mesh.materials) == 0:
                if bpy.data.materials.get('i3d_default_material') is None:
                    bpy.data.materials.new('i3d_default_material')

                mesh.materials.append(bpy.data.materials.get('i3d_default_material'))

            # Group triangles by subset, since they need to be exported in correct order per material subset to the i3d
            triangle_subsets = {}
            for triangle in mesh.loop_triangles:
                triangle_material = mesh.materials[triangle.material_index]
                if triangle_material.name not in triangle_subsets:
                    triangle_subsets[triangle_material.name] = []
                    # Add material to material section in i3d file and append to the materialIds that the shape
                    # object should have
                    material_id = self._xml_add_material(triangle_material)
                    if shape_id in self.shape_material_indexes.keys():
                        self.shape_material_indexes[shape_id] += f",{material_id:d}"
                    else:
                        self.shape_material_indexes[shape_id] = f"{material_id:d}"

                # Add triangle to subset
                triangle_subsets[triangle_material.name].append(triangle)

            self._xml_write_int(subsets_element, 'count', len(triangle_subsets))

            added_vertices = {}  # Key is a unique hashable vertex identifier and the value is a vertex index
            vertex_counter = 0  # Count the total number of unique vertices (total across all subsets)
            indices_total = 0  # Total amount of indices, since i3d format needs this number (for some reason)

            # Vertices are written to the i3d vertex list in an order based on the subsets and the triangles then index
            # into this list to define themselves
            for mat, subset in triangle_subsets.items():
                number_of_indices = 0
                number_of_vertices = 0
                subset_element = ET.SubElement(subsets_element, 'Subset')
                self._xml_write_int(subset_element, 'firstIndex', indices_total)
                self._xml_write_int(subset_element, 'firstVertex', vertex_counter)

                # Go through every triangle on the subset and extract triangle information
                i = 0
                for triangle in subset:
                    triangle_element = ET.SubElement(triangles_element, 't')
                    # Go through every loop that the triangle consists of and extract vertex information
                    triangle_vertex_index = ""  # The vertices from the vertex list that specify this triangle
                    for loop_index in triangle.loops:
                        vertex = mesh.vertices[mesh.loops[loop_index].vertex_index]
                        normal = mesh.loops[loop_index].normal
                        vertex_data = {'p': f"{vertex.co.xyz[0]:.6f} "
                                            f"{vertex.co.xyz[1]:.6f} "
                                            f"{vertex.co.xyz[2]:.6f}",

                                       'n': f"{normal.xyz[0]:.6f} "
                                            f"{normal.xyz[1]:.6f} "
                                            f"{normal.xyz[2]:.6f}",

                                       'uvs': {}
                                       }

                        # TODO: Check uv limit in GE
                        # Old addon only supported 4
                        for count, uv in enumerate(mesh.uv_layers):
                            if count < 4:
                                self._xml_write_bool(vertices_element, f'uv{count}', True)
                                vertex_data['uvs'][f't{count:d}'] = f"{uv.data[loop_index].uv[0]:.6f} " \
                                                                    f"{uv.data[loop_index].uv[1]:.6f}"
                            else:
                                print(f"Currently only supports four uv layers per vertex")

                        vertex_item = VertexItem(vertex_data, mat)

                        if vertex_item not in added_vertices:
                            added_vertices[vertex_item] = vertex_counter

                            vertex_element = ET.SubElement(vertices_element, 'v')
                            self._xml_write_string(vertex_element, 'n', vertex_data['n'])
                            self._xml_write_string(vertex_element, 'p', vertex_data['p'])

                            for uv_key, uv_data in vertex_data['uvs'].items():
                                self._xml_write_string(vertex_element, uv_key, uv_data)

                            vertex_counter += 1
                            number_of_vertices += 1

                        triangle_vertex_index += f"{added_vertices[vertex_item]} "

                    number_of_indices += 3  # 3 loops = 3 indices per triangle
                    self._xml_write_string(triangle_element, 'vi', triangle_vertex_index.strip(' '))

                self._xml_write_int(subset_element, 'numIndices', number_of_indices)
                self._xml_write_int(subset_element, 'numVertices', number_of_vertices)
                indices_total += number_of_indices

            self._xml_write_int(vertices_element, 'count', vertex_counter)
            self._xml_write_bool(vertices_element, 'normal', True)
            self._xml_write_bool(vertices_element, 'tangent', True)

            obj.to_mesh_clear()  # Clean out the generated mesh so it does not stay in blender memory

            # TODO: Write mesh related attributes
        else:
            # Mesh already exists, so find its shape it.
            shape_id = int(indexed_triangle_element.get('shapeId'))

        self._xml_write_int(node_element, 'shapeId', shape_id)
        self._xml_write_string(node_element, 'materialIds', self.shape_material_indexes[shape_id])

    def _xml_scene_object_transform_group(self, node: SceneGraph.Node, node_element: ET.Element):
        # TODO: Add parameters to UI and extract here
        pass

    def _xml_scene_object_camera(self, node: SceneGraph.Node, node_element: ET.Element):
        camera = node.blender_object.data

        self._xml_write_float(node_element, 'fov', camera.lens)
        self._xml_write_float(node_element, 'nearClip', camera.clip_start)
        self._xml_write_float(node_element, 'farClip', camera.clip_end)
        if camera.type == 'ORTHO':
            self._xml_write_bool(node_element, 'orthographic', True)
            self._xml_write_float(node_element, 'orthographicHeight', camera.ortho_scale)

    def _xml_scene_object_light(self, node: SceneGraph.Node, node_element: ET.Element):
        light = node.blender_object.data
        light_type = light.type
        falloff_type = None
        if light_type == 'POINT':
            light_type = 'point'
            falloff_type = light.falloff_type
        elif light_type == 'SUN':
            light_type = 'directional'
        elif light_type == 'SPOT':
            light_type = 'spot'
            falloff_type = light.falloff_type
            self._xml_write_float(node_element, 'coneAngle', math.degrees(light.spot_size))
            # Blender spot 0.0 -> 1.0, GE spot 0.0 -> 5.0
            self._xml_write_float(node_element, 'dropOff', 5.0 * light.spot_blend)
        elif light_type == 'AREA':
            light_type = 'point'
            print('Area lights not supported in giants engine, defaulting to point')

        self._xml_write_string(node_element, 'type', light_type)
        self._xml_write_string(node_element, 'color', "{0:f} {1:f} {2:f}".format(*light.color))
        self._xml_write_float(node_element, 'range', light.distance)
        self._xml_write_bool(node_element, 'castShadowMap', light.use_shadow)

        if falloff_type is not None:
            if falloff_type == 'CONSTANT':
                falloff_type = 0
            elif falloff_type == 'INVERSE_LINEAR':
                falloff_type = 1
            elif falloff_type == 'INVERSE_SQUARE':
                falloff_type = 2
            self._xml_write_int(node_element, 'decayRate', falloff_type)

    def _xml_export_to_file(self) -> None:
        self._indent(self._tree)  # Make the xml human readable by adding indents
        try:
            ET.ElementTree(self._tree).write(self._filepath, xml_declaration=True, encoding='iso-8859-1', method='xml')
            print(f"Exported to {self._filepath}")
        except Exception as exception:  # A bit slouchy exception handling. Should be more specific and not catch all
            print(exception)

    @staticmethod
    def _xml_write_int(element: ET.Element, attribute: str, value: int) -> None:
        """Write the attribute into the element with formatting for ints"""
        element.set(attribute, f"{value:d}")

    @staticmethod
    def _xml_write_float(element: ET.Element, attribute: str, value: float) -> None:
        """Write the attribute into the element with formatting for floats"""
        element.set(attribute, f"{value:.7f}")

    @staticmethod
    def _xml_write_bool(element: ET.Element, attribute: str, value: bool) -> None:
        """Write the attribute into the element with formatting for booleans"""
        element.set(attribute, f"{value!s}".lower())

    @staticmethod
    def _xml_write_string(element: ET.Element, attribute: str, value: str) -> None:
        """Write the attribute into the element with formatting for strings"""
        element.set(attribute, value)

    @staticmethod
    def _indent(elem: ET.Element, level: int = 0) -> None:
        """
        Used for pretty printing the xml since etree does not indent elements and keeps everything in one continues
        string and since i3d files are supposed to be human readable, we need indentation. There is a patch for
        pretty printing on its way in the standard library, but it is not available until python 3.9 comes around.

        The module 'lxml' could also be used since it has pretty-printing, but that would introduce an external
        library dependency for the addon.

        The source code from this solution is taken from http://effbot.org/zone/element-lib.htm#prettyprint

        It recursively checks every element and adds a newline + space indents to the element to make it pretty and
        easily readable. This technically changes the xml, but the giants engine does not seem to mind the linebreaks
        and spaces, when parsing the i3d file.
        """
        indents = '\n' + level * '  '
        if len(elem):
            if not elem.text or not elem.text.strip():
                elem.text = indents + '  '
            if not elem.tail or not elem.tail.strip():
                elem.tail = indents
            for elem in elem:
                Exporter._indent(elem, level + 1)
            if not elem.tail or not elem.tail.strip():
                elem.tail = indents
        else:
            if level and (not elem.tail or not elem.tail.strip()):
                elem.tail = indents

    @staticmethod
    def blender_to_i3d(blender_object: Union[bpy.types.Object, bpy.types.Collection]):
        # Collections don't have an object type since they aren't objects. If they are used for organisational purposes
        # they are converted into transformgroups in the scenegraph
        if isinstance(blender_object, bpy.types.Collection):
            return 'TransformGroup'

        switcher = {
            'MESH': 'Shape',
            'CURVE': 'Shape',
            'EMPTY': 'TransformGroup',
            'CAMERA': 'Camera',
            'LIGHT': 'Light',
            'COLLECTION': 'TransformGroup'
        }
        return switcher[blender_object.type]


class SceneGraph(object):
    class Node(object):
        def __init__(self,
                     node_id: int = 0,
                     blender_object: Union[bpy.types.Object, bpy.types.Collection] = None,
                     parent: SceneGraph.Node = None):
            self.children = {}
            self.blender_object = blender_object
            self.id = node_id
            self.parent = parent

            if parent is not None:
                parent.add_child(self)

        def __str__(self):
            return f"{self.id}|{self.blender_object.name!r}"

        def add_child(self, node: SceneGraph.Node):
            self.children[node.id] = node

        def remove_child(self, node: SceneGraph.Node):
            del self.children[node.id]

    def __init__(self):
        self.ids = {
            'node': 0
        }
        self.nodes = {}
        self.shapes = {}
        self.materials = {}
        self.files = {}
        # Create the root node
        self.add_node()  # Add the root node that contains the tree

    def __str__(self):
        """Tree represented as depth first"""
        tree_string = ""
        longest_string = 0

        def traverse(node, indents=0):
            nonlocal tree_string, longest_string
            indent = indents * '  '
            line = f"|{indent}{node}\n"
            longest_string = len(line) if len(line) > longest_string else longest_string
            tree_string += line
            for child in node.children.values():
                traverse(child, indents + 1)

        traverse(self.nodes[1])  # Start at the first element instead since the root isn't necessary to print out

        tree_string += f"{longest_string * '-'}\n"

        return f"{longest_string * '-'}\n" + tree_string

    def add_node(self,
                 blender_object: Union[bpy.types.Object, bpy.types.Collection] = None,
                 parent: SceneGraph.Node = None) -> SceneGraph.Node:
        new_node = SceneGraph.Node(self.ids['node'], blender_object, parent)
        self.nodes[new_node.id] = new_node
        self.ids['node'] += 1
        return new_node


class VertexItem:
    """Define unique vertex items (Could be the same vertex but with a different color or material uv"""

    def __init__(self, vertex_item, material_name):
        self._str = f"{material_name}"
        for key, item in vertex_item.items():
            self._str += f" {item}"

    def __str__(self):
        return self._str

    def __hash__(self):
        return hash(self._str)

    def __eq__(self, other):
        return self._str == f'{other!s}'
