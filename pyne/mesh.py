from __future__ import print_function, division
import sys
import copy
import itertools
from collections import Iterable, Sequence
from warnings import warn
from pyne.utils import QAWarning

import numpy as np
import tables as tb

warn(__name__ + " is not yet QA compliant.", QAWarning)

try:
#    from itaps import iMesh, iBase, iMeshExtensions
    HAVE_PYTAPS = True

except ImportError:
    warn("the PyTAPS optional dependency could not be imported. "
         "Some aspects of the mesh module may be incomplete.", QAWarning)
    HAVE_PYTAPS = False

from pyne.material import Material, MaterialLibrary, MultiMaterial

from pymoab import core, hcoord, scd, types
from pymoab.rng import subtract
from pymoab.tag import Tag

_BOX_DIMS_TAG_NAME = "BOX_DIMS"

if sys.version_info[0] > 2:
    basestring = str

# dictionary of lamba functions for mesh arithmetic
_ops = {"+": lambda val_1, val_2: (val_1 + val_2),
        "-": lambda val_1, val_2: (val_1 - val_2),
        "*": lambda val_1, val_2: (val_1 * val_2),
        "/": lambda val_1, val_2: (val_1 / val_2)}

err__ops = {"+": lambda val_1, val_2, val_1_err, val_2_err:
                 (1/(val_1 + val_2)*np.sqrt((val_1*val_1_err)**2
                  + (val_2*val_2_err)**2)),
            "-": lambda val_1, val_2, val_1_err, val_2_err:
                 (1/(val_1 - val_2)*np.sqrt((val_1*val_1_err)**2
                  + (val_2*val_2_err)**2)),
            "*": lambda val_1, val_2, val_1_err, val_2_err:
                 (np.sqrt(val_1_err**2 + val_2_err**2)),
            "/": lambda val_1, val_2, val_1_err, val_2_err:
                 (np.sqrt(val_1_err**2 + val_2_err**2))}

_INTEGRAL_TYPES = (int, np.integer, np.bool_)
_SEQUENCE_TYPES = (Sequence, np.ndarray)


class Tag(object):
    """A mesh tag, which acts as a descriptor on the mesh.  This dispatches
    access to intrinsic material properties, the iMesh.Mesh tags, and material
    metadata attributes.
    """

    def __init__(self, mesh=None, name=None, doc=None):
        """Parameters
        ----------
        mesh : Mesh, optional
            The PyNE mesh to tag.
        name : str, optional
            The name of the tag.
        doc : str, optional
            Documentation string for the tag.

        """
        if mesh is None or name is None:
            self._lazy_args = {'mesh': mesh, 'name': name, 'doc': doc}
            return
        self.mesh = mesh
        self.name = name
        mesh.tags[name] = self
        if doc is None:
            doc = "the {0!r} tag".format(name)
        self.__doc__ = doc
        if hasattr(self, '_lazy_args'):
            del self._lazy_args

    def __str__(self):
        return "{0}: {1}".format(self.__class__.__name__, self.name)

    def __repr__(self):
        return "{0}(name={1!r}, doc={2!r})".format(self.__class__.__name__, self.name,
                                                   self.__doc__)

    def __get__(self, mesh, objtype=None):
        return self

    def __set__(self, mesh, value):
        if not isinstance(value, Tag):
            raise AttributeError("can't set tag from non-tag objects, "
                                 "got {0}".format(type(value)))
        if self.name != value.name:
            raise AttributeError("tags names must match, found "
                                 "{0} and {1}".format(self.name, value.name))
        self[:] = value[:]

    def __delete__(self, mesh):
        del self[:]


class MaterialPropertyTag(Tag):
    """A mesh tag which looks itself up as a material property (attribute).
    This makes the following expressions equivalent for a given material property
    name::

        mesh.name[i] == mesh.mats[i].name

    It also adds slicing, fancy indexing, boolean masking, and broadcasting
    features to this process.
    """

    def __getitem__(self, key):
        name = self.name
        mats = self.mesh.mats
        if mats is None:
            RuntimeError("Mesh.mats is None, please add a MaterialLibrary.")
        size = len(self.mesh)
        if isinstance(key, _INTEGRAL_TYPES):
            return getattr(mats[key], name)
        elif isinstance(key, slice):
            return np.array([getattr(mats[i], name)
                            for i in range(*key.indices(size))])
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match the length of the mesh.")
            return np.array([getattr(mats[i], name) for i, b in enumerate(key)
                            if b])
        elif isinstance(key, Iterable):
            return np.array([getattr(mats[i], name) for i in key])
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def __setitem__(self, key, value):
        name = self.name
        mats = self.mesh.mats
        if mats is None:
            RuntimeError("Mesh.mats is None, please add a MaterialLibrary.")
        size = len(self.mesh)
        if isinstance(key, _INTEGRAL_TYPES):
            setattr(mats[key], name, value)
        elif isinstance(key, slice):
            idx = range(*key.indices(size))
            if isinstance(value, _SEQUENCE_TYPES) and len(value) == len(idx):
                for i, v in zip(idx, value):
                    setattr(mats[i], name, v)
            else:
                for i in idx:
                    setattr(mats[i], name, value)
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match "
                               "the length of the mesh.")
            idx = np.where(key)[0]
            if isinstance(value, _SEQUENCE_TYPES) and len(value) == key.sum():
                for i, v in zip(idx, value):
                    setattr(mats[i], name, v)
            else:
                for i in idx:
                    setattr(mats[i], name, value)
        elif isinstance(key, Iterable):
            if isinstance(value, _SEQUENCE_TYPES) and len(value) == len(key):
                for i, v in zip(key, value):
                    setattr(mats[i], name, v)
            else:
                for i in key:
                    setattr(mats[i], name, value)
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def __delitem__(self, key):
        msg = ("the material property tag {0!r} may "
               "not be deleted").format(self.name)
        raise AttributeError(msg)


class MaterialMethodTag(Tag):
    """A mesh tag which looks itself up by calling a material method which takes
    no arguments.  This makes the following expressions equivalent for a given
    material method name::

        mesh.name[i] == mesh.mats[i].name()

    It also adds slicing, fancy indexing, boolean masking, and broadcasting
    features to this process.
    """

    def __getitem__(self, key):
        name = self.name
        mats = self.mesh.mats
        if mats is None:
            RuntimeError("Mesh.mats is None, please add a MaterialLibrary.")
        size = len(self.mesh)
        if isinstance(key, _INTEGRAL_TYPES):
            return getattr(mats[key], name)()
        elif isinstance(key, slice):
            return np.array([getattr(mats[i], name)() for i in
                             range(*key.indices(size))])
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match the "
                               "length of the mesh.")
            return np.array([getattr(mats[i], name)() for i, b in
                             enumerate(key) if b])
        elif isinstance(key, Iterable):
            return np.array([getattr(mats[i], name)() for i in key])
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def __setitem__(self, key, value):
        msg = "the material method tag {0!r} may not be set".format(self.name)
        raise AttributeError(msg)

    def __delitem__(self, key):
        msg = ("the material method tag {0!r} may not be "
               "deleted").format(self.name)
        raise AttributeError(msg)


class MetadataTag(Tag):
    """A mesh tag which looks itself up as a material metadata attribute.
    Tags of this are untyped and may have any size.  Use this for catch-all
    tags. This makes the following expressions equivalent for a given material
    property.

    name::

        mesh.name[i] == mesh.mats[i].metadata['name']

    It also adds slicing, fancy indexing, boolean masking, and broadcasting
    features to this process.
    """

    def __getitem__(self, key):
        name = self.name
        mats = self.mesh.mats
        if mats is None:
            RuntimeError("Mesh.mats is None, please add a MaterialLibrary.")
        size = len(self.mesh)
        if isinstance(key, _INTEGRAL_TYPES):
            return mats[key].metadata[name]
        elif isinstance(key, slice):
            return [mats[i].metadata[name] for i in range(*key.indices(size))]
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match the length "
                               "of the mesh.")
            return [mats[i].metadata[name] for i, b in enumerate(key) if b]
        elif isinstance(key, Iterable):
            return [mats[i].metadata[name] for i in key]
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def __setitem__(self, key, value):
        name = self.name
        mats = self.mesh.mats
        if mats is None:
            RuntimeError("Mesh.mats is None, please add a MaterialLibrary.")
        size = len(self.mesh)
        if isinstance(key, _INTEGRAL_TYPES):
            mats[key].metadata[name] = value
        elif isinstance(key, slice):
            idx = range(*key.indices(size))
            if isinstance(value, _SEQUENCE_TYPES) and len(value) == len(idx):
                for i, v in zip(idx, value):
                    mats[i].metadata[name] = v
            else:
                for i in idx:
                    mats[i].metadata[name] = value
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match the length "
                               "of the mesh.")
            idx = np.where(key)[0]
            if isinstance(value, _SEQUENCE_TYPES) and len(value) == key.sum():
                for i, v in zip(idx, value):
                    mats[i].metadata[name] = v
            else:
                for i in idx:
                    mats[i].metadata[name] = value
        elif isinstance(key, Iterable):
            if isinstance(value, _SEQUENCE_TYPES) and len(value) == len(key):
                for i, v in zip(key, value):
                    mats[i].metadata[name] = v
            else:
                for i in key:
                    mats[i].metadata[name] = value
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def __delitem__(self, key):
        name = self.name
        mats = self.mesh.mats
        if mats is None:
            RuntimeError("Mesh.mats is None, please add a MaterialLibrary.")
        size = len(self.mesh)
        if isinstance(key, _INTEGRAL_TYPES):
            del mats[key].metadata[name]
        elif isinstance(key, slice):
            for i in range(*key.indices(size)):
                del mats[i].metadata[name]
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match the length "
                               "of the mesh.")
            for i, b in enumerate(key):
                if b:
                    del mats[i].metadata[name]
        elif isinstance(key, Iterable):
            for i in key:
                del mats[i].metadata[name]
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))


class IMeshTag(Tag):
    """A mesh tag which looks itself up as a tag on the iMesh.Mesh instance.
    This makes the following expressions equivalent for a given iMesh.Mesh tag
    name::

        mesh.name[i] == mesh.mesh.getTagHandle(name)[list(mesh.mesh.iterate(
                                iBase.Type.region, iMesh.Topology.all))[i]]

    It also adds slicing, fancy indexing, boolean masking, and broadcasting
    features to this process.
    """

    def __init__(self, size = 1, dtype = 'f8', default = 0.0, mesh = None, name = None,
                 doc=None):
        """Parameters
        ----------
        size : int, optional
            The number of elements of type dtype that this tag stores.
        dtype : np.dtype or similar, optional
            The data type of this tag from int, float, and byte. See PyTAPS
            tags for more details.
        default : dtype or None, optional
            The default value to fill this tag with upon creation. If None,
            then the tag is created empty.
        mesh : Mesh, optional
            The PyNE mesh to tag.
        name : str, optional
            The name of the tag.
        doc : str, optional
            Documentation string for the tag.

        """

        super(IMeshTag, self).__init__(mesh=mesh, name=name, doc=doc)
        if mesh is None or name is None:
            self._lazy_args['size'] = size
            self._lazy_args['dtype'] = dtype
            self._lazy_args['default'] = default
            return
        self.size = size
        self.dtype = dtype
        self.pymbtype = types.pymoab_data_type(self.dtype)
        self.default = default
        try:
            self.tag = self.mesh.mesh.tag_get_handle(self.name)
        except RuntimeError:
            self.tag = self.mesh.mesh.tag_get_handle(self.name, size, self.pymbtype, types.MB_TAG_DENSE, create_if_missing = True, default_value = default)
            if default is not None:
                self[:] = default

    def __delete__(self, mesh):
        super(IMeshTag, self).__delete__(mesh)
        self.mesh.mesh.tag_delete(self.name)

    def __getitem__(self, key):
        m = self.mesh.mesh
        size = len(self.mesh)
        mtag = self.tag
        miter = self.mesh.iter_ve()
        if isinstance(key, long):
            return self.mesh.mesh.tag_get_data(self.tag, key, flat = True)
        elif isinstance(key, _INTEGRAL_TYPES):
            if key >= size:
                raise IndexError("key index {0} greater than the size of the "
                                 "mesh {1}".format(key, size))
            for i_ve in zip(range(key+1), miter):
                pass
            return self.mesh.mesh.tag_get_data(self.tag, i_ve[1], flat = True)
        elif isinstance(key, slice):
            flat = True if self.size == 1 else False
            return self.mesh.mesh.tag_get_data(self.tag, list(miter), flat = flat)[key]
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match the length "
                               "of the mesh.")
            return self.mesh.mesh.tag_get_data(self.tag, [ve for b, ve in zip(key, miter) if b], flat = True)
        elif isinstance(key, Iterable):
            ves = list(miter)
            ves_to_get = []
            # support either indexes or entityhandles
            for k in key:
                if isinstance(int):
                    ves_to_get.append(ves[k])
                elif isinstance(long):
                    ves_to_get.append(k)
                else:
                    raise TypeError("{0} contains invalid element references "
                                    "(non-ints, non-handles)".format(key))
            return self.mesh.mesh.tag_get_data(self.tag, ves_to_get, flat = True)

        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def __setitem__(self, key, value):
        # get value into canonical form
        tsize = self.size
        value = np.asarray(value, self.tag.get_dtype())
        value = np.atleast_1d(value) if tsize == 1 else np.atleast_2d(value)
        # set up mesh to be iterated over
        m = self.mesh.mesh
        msize = len(self.mesh)
        mtag = self.tag
        miter = self.mesh.iter_ve()
        if isinstance(key, long):
            self.mesh.mesh.tag_set_data(self.tag, key, value)
        elif isinstance(key, _INTEGRAL_TYPES):
            if key >= msize:
                raise IndexError("key index {0} greater than the size of the "
                                 "mesh {1}".format(key, msize))
            for i_ve in zip(range(key+1), miter):
                pass
            self.mesh.mesh.tag_set_data(self.tag, i_ve[1], value if tsize == 1 else value[0])
        elif isinstance(key, slice):
            key = list(miter)[key]
            v = np.empty((len(key), tsize), self.tag.get_dtype())
            if tsize == 1 and len(value.shape) == 1:
                v.shape = (len(key), )
            v[...] = value
            self.mesh.mesh.tag_set_data(mtag,key,v)
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != msize:
                raise KeyError("boolean mask must match the length "
                               "of the mesh.")
            key = [ve for b, ve in zip(key, miter) if b]
            v = np.empty((len(key), tsize), self.tag.get_dtype())
            if tsize == 1 and len(value.shape) == 1:
                v.shape = (len(key), )
            v[...] = value
            self.mesh.mesh.tag_set_data(mtag, key, v)
        elif isinstance(key, Iterable):
            ves = list(miter)
            if tsize != 1 and len(value) != len(key):
                v = np.empty((len(key), tsize), self.tag.get_dtype())
                v[...] = value
                value = v
            ves_to_tag = []
            # support either indexes or entityhandles
            for k in key:
                if isinstance(int):
                    ves_to_tag.append(ves[k])
                elif isinstance(long):
                    ves_to_tag.append(k)
                else:
                    raise TypeError("{0} contains invalid element references "
                                    "(non-ints, non-handles)".format(key))
            self.mesh.mesh.tag_set_data(mtag, ves_to_, value)
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def __delitem__(self, key):
        m = self.mesh.mesh
        size = len(self.mesh)
        mtag = self.tag
        miter = self.mesh.iter_ve()
        if isinstance(key, _INTEGRAL_TYPES):
            if key >= size:
                raise IndexError("key index {0} greater than the size of the "
                                 "mesh {1}".format(key, size))
            for i_ve in zip(range(key+1), miter):
                pass
            self.mesh.mesh.tag_delete_data(mtag, i_ve[1])
        elif isinstance(key, slice):
            self.mesh.mesh.tag_delete_data(mtag, list(miter)[key])
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match the "
                               "length of the mesh.")
            self.mesh.mesh.tag_delete_data(mtag,[ve for b, ve in zip(key, miter) if b])
        elif isinstance(key, Iterable):
            ves = list(miter)
            ves_to_delt = []
            # support either indexes or entityhandles
            for k in key:
                if isinstance(int):
                    ves_to_del.append(ves[k])
                elif isinstance(long):
                    ves_to_del.append(k)
                else:
                    raise TypeError("{0} contains invalid element references "
                                    "(non-ints, non-handles)".format(key))
            self.mesh.mesh.tag_delete_data(mtag,ves_to_del)
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def expand(self):
        """This function creates a group of scalar tags from a vector tag. For
        a vector tag named <tag_name> of length N, scalar tags in the form:

        <tag_name>_000, <tag_name>_001, <tag_name>_002... <tag_name>_N

        are created and the data is tagged accordingly.
        """
        if self.size < 2:
            raise TypeError("Cannot expand a tag that is already a scalar.")
        for j in range(self.size):
            data = [x[j] for x in self[:]]
            tag = self.mesh.mesh.tag_get_handle("{0}_{1:03d}".format(self.name, j),
                                                1, self.pymbtype, storage_type = types.MB_TAG_DENSE, create_if_missing = True)
            self.mesh.mesh.tag_set_data(tag, list(self.mesh.iter_ve()), data)


class ComputedTag(Tag):
    '''A mesh tag which looks itself up by calling a function (or other callable)
    with the following signature::

        def f(mesh, i):
            """mesh is a pyne.mesh.Mesh() object and i is the volume element
            index to compute.
            """
            # ... do some work ...
            return anything_you_want

    This makes the following expressions equivalent for a given computed tag
    name::

        mesh.name[i] == f(mesh, i)

    It also adds slicing, fancy indexing, boolean masking, and broadcasting
    features to this process.

    Notes
    -----
    The results of computed tags are not stored and the function object itself
    is also not persisted.  Therefore, you must manually re-tag the mesh with
    the desired functions each session.

    '''

    def __init__(self, f, mesh=None, name=None, doc=None):
        """Parameters
        ----------
        f : callable object
            The function that performs the computation.
        mesh : Mesh, optional
            The PyNE mesh to tag.
        name : str, optional
            The name of the tag.
        doc : str, optional
            Documentation string for the tag.

        """
        doc = doc or f.__doc__
        super(ComputedTag, self).__init__(mesh=mesh, name=name, doc=doc)
        if mesh is None or name is None:
            self._lazy_args['f'] = f
            return
        self.f = f

    def __getitem__(self, key):
        m = self.mesh
        f = self.f
        size = len(m)
        if isinstance(key, _INTEGRAL_TYPES):
            if key >= size:
                raise IndexError("key index {0} greater than the size of the "
                                 "mesh {1}".format(key, size))
            return f(m, key)
        elif isinstance(key, slice):
            return [f(m, i) for i in range(*key.indices(size))]
        elif isinstance(key, np.ndarray) and key.dtype == np.bool:
            if len(key) != size:
                raise KeyError("boolean mask must match the length "
                               "of the mesh.")
            return [f(m, i) for i, b in enumerate(key) if b]
        elif isinstance(key, Iterable):
            return [f(m, i) for i in key]
        else:
            raise TypeError("{0} is not an int, slice, mask, "
                            "or fancy index.".format(key))

    def __setitem__(self, key, value):
        msg = "the computed tag {0!r} may not be set".format(self.name)
        raise AttributeError(msg)

    def __delitem__(self, key):
        msg = "the computed tag {0!r} may not be deleted".format(self.name)
        raise AttributeError(msg)


class MeshError(Exception):
    """Errors related to instantiating mesh objects and utilizing their methods.
    """
    pass

class Mesh(object):
    """This class houses an iMesh instance and contains methods for various mesh
    operations. Special methods exploit the properties of structured mesh.

    Attributes
    ----------
    mesh : iMesh instance
    structured : bool
        True for structured mesh.
    structured_coords : list of lists
        A list containing lists of x_points, y_points and z_points that make up
        a structured mesh.
    structured_ordering : str
        A three character string denoting the iteration order of the mesh (e.g.
        'xyz', meaning z changest fastest, then y, then x.)
    """

    def __init__(self, mesh=None, structured=False,
                 structured_coords=None, structured_set=None,
                 structured_ordering='xyz', mats=()):
        """Parameters
        ----------
        mesh : iMesh instance or str, optional
            Either an iMesh instance or a file name of file containing an
            iMesh instance.
        structured : bool, optional
            True for structured mesh.
        structured_coords : list of lists, optional
            A list containing lists of x_points, y_points and z_points
            that make up a structured mesh.
        structured_set : iMesh entity set handle, optional
            A preexisting structured entity set on an iMesh instance with a
            "BOX_DIMS" tag.
        structured_ordering : str, optional
            A three character string denoting the iteration order of the mesh
            (e.g. 'xyz', meaning z changest fastest, then y, then x.)
        mats : MaterialLibrary or dict or Materials or None, optional
            This is a mapping of volume element handles to Material objects.
            If mats is None, then no empty materials are created for the mesh.

            Unstructured mesh instantiation:
                 - From iMesh instance by specifying: <mesh>
                 - From mesh file by specifying: <mesh_file>

            Structured mesh instantiation:
                - From iMesh instance with exactly 1 entity set (with BOX_DIMS
                  tag) by specifying <mesh> and structured = True.
                - From mesh file with exactly 1 entity set (with BOX_DIMS tag)
                  by specifying <mesh_file> and structured = True.
                - From an imesh instance with multiple entity sets by
                  specifying <mesh>, <structured_set>, structured=True.
                - From coordinates by specifying <structured_coords>,
                  structured=True, and optional preexisting iMesh instance
                  <mesh>

            The "BOX_DIMS" tag on iMesh instances containing structured mesh is
            a vector of floats it the following form:
            [i_min, j_min, k_min, i_max, j_max, k_max]
            where each value is a volume element index number. Typically volume
            elements should be indexed from 0. The "BOX_DIMS" information is
            stored in self.dims.

        """
        if mesh is None:
            self.mesh = core.Core()
        elif isinstance(mesh, basestring):
            self.mesh = core.Core()
            self.mesh.load_file(mesh)
        else:
            self.mesh = mesh

        self.structured = structured

        if self.structured:
            self.scd = scd.ScdInterface(self.mesh)
            self.structured_coords = structured_coords
            self.structured_ordering = structured_ordering
            # if a MOAB mesh instance exists and no structured coords
            # or structured set is provided, search for a single
            # structured set

            ##### TO-DO: SHOULD BE REPLACED WITH scd.find_boxes AT SOME POINT #####
            if (mesh is not None) and not structured_coords \
               and not structured_set:
                # check for the structured box tag on the instance
                try:
                    box_tag = self.mesh.tag_get_handle(_BOX_DIMS_TAG_NAME)
                except types.MB_TAG_NOT_FOUND as e:
                    print("BOX_DIMS not found on MOAB mesh instance")
                    raise e

                # find all entity sets with the structured box tag
                count = 0
                root_set = self.mesh.get_root_set()
                for ent_set in self.mesh.get_entities_by_type(root_set, types.MBENTITYSET):
                    try:
                        self.mesh.tag_get_data(box_tag, ent_set)
                    except:
                        pass
                    else:
                        self.structured_set = ent_set
                        count += 1

                if count == 0:
                    raise MeshError("Found no structured meshes in "
                                    "file {0}".format(mesh))
                elif count > 1:
                    raise MeshError("Found {0} structured meshes."
                                    " Instantiate individually using"
                                    " from_ent_set()".format(count))

            # from coordinates
            elif (mesh is None) and structured_coords and not structured_set:
                # check for single vertex coordinates here? it seems we only support volumetric mesh -PCS
                extents = [0, 0, 0] + [len(x) - 1 for x in structured_coords]
                low = hcoord.HomCoord([0,0,0])
                high = hcoord.HomCoord([len(x) - 1 for x in structured_coords])
                ### TO-DO: generation of explicit vertex coords could be more efficient ###
                coords = []
                for z in range(low[2], high[2]+1):
                    for y in range(low[1], high[1]+1):
                        for x in range(low[0], high[0]+1):
                           coords.append(structured_coords[0][x])
                           coords.append(structured_coords[1][y])
                           coords.append(structured_coords[2][z])

                scd_box = self.scd.construct_box(low, high, coords)
                self.structured_set = scd_box.box_set()

            # from mesh and structured_set:
            elif not structured_coords and structured_set:
                # check for the structured box tag on the instance
                try:
                    box_tag = self.mesh.tag_get_handle(_BOX_DIMS_TAG_NAME)
                except types.MB_TAG_NOT_FOUND as e:
                    print("BOX_DIMS not found on MOAB mesh instance")
                    raise e

                try:
                    self.mesh.tag_get_data(box_tag, structured_set)
                except:
                    print("Supplied entity set does not contain BOX_DIMS tag")
                    raise e

                self.structured_set = structured_set
            else:
                raise MeshError("For structured mesh instantiation, need to"
                                "supply exactly one of the following:\n"
                                "A. PyMOAB instance\n"
                                "B. Mesh file\n"
                                "C. Mesh coordinates\n"
                                "D. Structured entity set AND PyMOAB instance")

            self.dims = list(self.mesh.tag_get_data(self.mesh.tag_get_handle("BOX_DIMS"),
                                                    self.structured_set, flat = True))

            self.vertex_dims = list(self.dims[0:3]) \
                               + [x + 1 for x in self.dims[3:6]]

            if self.structured_coords is None:
                self.structured_coords = [self.structured_get_divisions("x"),
                                          self.structured_get_divisions("y"),
                                          self.structured_get_divisions("z")]
        else:
            # Unstructured mesh cases
            # Error if structured arguments are passed
            if structured_coords or structured_set:
                MeshError("Structured mesh arguments should not be present for\
                            unstructured Mesh instantiation.")

        # sets mats
        mats_in_mesh_file = False
        if isinstance(mesh, basestring) and len(mats) == 0:
            with tb.open_file(mesh) as h5f:
                if '/materials' in h5f:
                    mats_in_mesh_file = True
            if mats_in_mesh_file:
                mats = MaterialLibrary(mesh)

        if mats is None:
            pass
        elif len(mats) == 0 and not mats_in_mesh_file:
            mats = MaterialLibrary()
        elif not isinstance(mats, MaterialLibrary):
            mats = MaterialLibrary(mats)

        self.mats = mats

        # tag with volume id and ensure mats exist.
        ves = list(self.iter_ve())
        tags = self.mesh.tag_get_tags_on_entity(ves[0])
        tag_idx = self.mesh.tag_get_handle('idx', 1,
                                           types.MB_TYPE_INTEGER,
                                           types.MB_TAG_DENSE,
                                           create_if_missing = True)

        # tag with volume id and ensure mats exist.
        ves = list(self.iter_ve())
        tags = self.mesh.tag_get_tags_on_entity(ves[0])
        tags = set(tag.get_name() for tag in tags)
        if 'idx' in tags:
            tag_idx = self.mesh.tag_get_handle('idx')
        else:
            tag_idx = self.mesh.tag_get_handle('idx', 1, types.MB_TYPE_INTEGER, types.MB_TAG_DENSE, create_if_missing = True)
        for i, ve in enumerate(ves):
            self.mesh.tag_set_data(tag_idx, ve, i)
            if mats is not None and i not in mats:
                mats[i] = Material()
        self._len = i + 1

        # Default tags
        self.tags = {}
        if mats is not None:
            # metadata tags, these should come first so they don't accidentally
            # overwite hard coded tag names.
            metatagnames = set()
            for mat in mats.values():
                metatagnames.update(mat.metadata.keys())
            for name in metatagnames:
                setattr(self, name, MetadataTag(mesh=self, name=name))
        # iMesh.Mesh() tags
        tagnames = set()
        for ve in ves:
            tagnames.update(t.get_name() for t in self.mesh.tag_get_tags_on_entity(ve))
        for name in tagnames:
            setattr(self, name, IMeshTag(mesh=self, name=name))

        if mats is not None:
            # Material property tags
            self.atoms_per_molecule = MaterialPropertyTag(mesh=self,
                                                          name='atoms_per_molecule',
                                                          doc='Number of atoms per molecule')
            self.metadata = MaterialPropertyTag(mesh=self, name='metadata',
                                                doc='metadata attributes, stored on the material')
            self.comp = MaterialPropertyTag(mesh=self, name='comp',
                                            doc='normalized composition mapping from nuclides to '
                                            'mass fractions')
            self.mass = MaterialPropertyTag(mesh=self, name='mass',
                                            doc='the mass of the material')
            self.density = MaterialPropertyTag(mesh=self, name='density',
                                               doc='the density [g/cc]')
            # Material method tags
            methtagnames = ('expand_elements', 'mass_density',
                            'molecular_mass', 'mult_by_mass',
                            'number_density', 'sub_act', 'sub_fp',
                            'sub_lan', 'sub_ma', 'sub_tru', 'to_atom_frac')
            for name in methtagnames:
                doc = "see Material.{0}() for more information".format(name)
                setattr(self, name, MaterialMethodTag(mesh=self, name=name,
                                                      doc=doc))

    def __len__(self):
        return self._len

    def __iter__(self):
        """Iterates through the mesh and at each step yield the volume element
        index i, the material mat, and the volume element itself ve.
        """
        mats = self.mats
        if mats is None:
            for i, ve in enumerate(self.iter_ve()):
                yield i, None, ve
        else:
            for i, ve in enumerate(self.iter_ve()):
                yield i, mats[i], ve

    def iter_ve(self):
        if self.structured:
            return self.structured_iterate_hex(self.structured_ordering)
        else:
            return self.mesh.get_entities_by_dimension(self.mesh.get_root_set(), 3, True)

    def __contains__(self, i):
        return i < len(self)

    def __setattr__(self, name, value):
        if isinstance(value, Tag) and hasattr(value, '_lazy_args'):
            # some 1337 1Azy 3\/a1
            kwargs = value._lazy_args
            kwargs['mesh'] = self if kwargs['mesh'] is None else kwargs['mesh']
            kwargs['name'] = name if kwargs['name'] is None else kwargs['name']
            value = type(value)(**kwargs)
        super(Mesh, self).__setattr__(name, value)

    def tag(self, name, value=None, tagtype=None, doc=None, size=None,
            dtype=None):
        """Adds a new tag to the mesh, guessing the approriate place to store
        the data.

        Parameters
        ----------
        name : str
            The tag name
        value : optional
            The value to initialize the tag with, skipped if None.
        tagtype : Tag or str, optional
            The type of tag this should be any of the following classes or
            strings are accepted: IMeshTag, MetadataTag, ComputedTag, 'imesh',
            'metadata', or 'computed'.
        doc : str, optional
            The tag documentation string.
        size : int, optional
            The size of the tag. This only applies to IMeshTags.
        dtype : numpy dtype, optional
            The data type of the tag. This only applies to IMeshTags. See PyTAPS
            for more details.

        """
        if name in self.tags:
            raise KeyError('{0} tag already exists on the mesh'.format(name))
        if tagtype is None:
            if callable(value):
                tagtype = ComputedTag
            elif size is None and dtype is not None:
                size = 1
                tagtype = IMeshTag
            elif size is not None and dtype is None:
                dtype = 'f8'
                tagtype = IMeshTag
            elif value is None:
                size = 1
                value = 0.0
                dtype = 'f8'
                tagtype = IMeshTag
            elif isinstance(value, float):
                dtype = 'f8'
                tagtype = IMeshTag
            elif isinstance(value, int):
                dtype = 'i'
                tagtype = IMeshTag
            elif isinstance(value, str):
                tagtype = MetadataTag
            elif isinstance(value, _SEQUENCE_TYPES):
                raise ValueError('ambiguous tag {0!r} creation when value is a'
                                 ' sequence, please set tagtype, size, '
                                 'or dtype'.format(name))
            else:
                tagtype = MetadataTag
        if tagtype is IMeshTag or tagtype.lower() == 'imesh':
            t = IMeshTag(size=size, dtype=dtype, mesh=self, name=name, doc=doc)
        elif tagtype is MetadataTag or tagtype.lower() == 'metadata':
            t = MetadataTag(mesh=self, name=name, doc=doc)
        elif tagtype is ComputedTag or tagtype.lower() == 'computed':
            t = ComputedTag(f=value, mesh=self, name=name, doc=doc)
        else:
            raise ValueError('tagtype {0} not valid'.format(tagtype))
        if value is not None and tagtype is not ComputedTag:
            t[:] = value
        setattr(self, name, t)


    def get_tag(self, tag_name):
        return getattr(self, tag_name)


    def __iadd__(self, other):
        """Adds the common tags of other to the mesh object.
        """
        tags = self.common_ve_tags(other)
        return self._do_op(other, tags, "+")

    def __isub__(self, other):
        """Substracts the common tags of other to the mesh object.
        """
        tags = self.common_ve_tags(other)
        return self._do_op(other, tags, "-")

    def __imul__(self, other):
        """Multiplies the common tags of other to the mesh object.
        """
        tags = self.common_ve_tags(other)
        return self._do_op(other, tags, "*")

    def __idiv__(self, other):
        """Divides the common tags of other to the mesh object.
        """
        tags = self.common_ve_tags(other)
        return self._do_op(other, tags, "/")

    def _do_op(self, other, tags, op, in_place=True):
        """Private function to do mesh +, -, *, /.
        """
        # Exclude error tags in a case a StatMesh is mistakenly initialized as
        # a Mesh object.
        tags = set(tag for tag in tags if not tag.endswith('_error'))

        if in_place:
            mesh_1 = self
        else:
            mesh_1 = copy.copy(self)
        for tag in tags:
            for ve_1, ve_2 in \
                zip(zip(iter(meshset_iterate(mesh_1.mesh, mesh_1.structured_set, types.MBMAXTYPE, dim = 3))),
                    zip(iter(meshset_iterate( other.mesh,  other.structured_set, types.MBMAXTYPE, dim = 3)))):
                mesh_1_tag = mesh_1.mesh.tag_get_handle(tag)
                other_tag  = other.mesh.tag_get_handle(tag)
                val = _ops[op](mesh_1.mesh.tag_get_data(mesh_1_tag, ve_1, flat = True)[0],
                         other.mesh.tag_get_data(other_tag,   ve_2, flat = True)[0])
                mesh_1.mesh.tag_set_data(mesh_1_tag, ve_1,
                    _ops[op](mesh_1.mesh.tag_get_data(mesh_1_tag, ve_1, flat = True)[0],
                             other.mesh.tag_get_data(other_tag,   ve_2, flat = True)[0]))

        return mesh_1

    def common_ve_tags(self, other):
        """Returns the volume element tags in common between self and other.
        """
        self_tags = self.mesh.tag_get_tags_on_entity(list(meshset_iterate(self.mesh, self.structured_set, types.MBMAXTYPE, dim = 3))[0])
        other_tags = other.mesh.tag_get_tags_on_entity(list(meshset_iterate(other.mesh, other.structured_set, types.MBMAXTYPE, dim = 3))[0])
        self_tags = set(x.get_name() for x in self_tags)
        other_tags = set(x.get_name() for x in other_tags)
        intersect = self_tags & other_tags
        intersect.discard('idx')
        return intersect

    def __copy__(self):
        # first copy full imesh instance
        pymb_copy = core.Core()

        # now create Mesh objected from copied iMesh instance
        mesh_copy = Mesh(mesh=pymb_copy,
                         structured=copy.copy(self.structured))
        return mesh_copy

    # Non-structured volume methods
    def elem_volume(self, ve):
        """Get the volume of a hexahedral or tetrahedral volume element

        Approaches are adapted from MOAB's measure.cpp.

        Parameters
        ----------
        ve : iMesh.Mesh.EntitySet
            A volume element

        Returns
        -------
        .. : float
            Element's volume. Returns None if volume is not a hex or tet.
        """
        coord = self.mesh.get_coords(self.mesh.get_connectivity(ve)).reshape(-1,3)
        num_coords = coord.shape[0]

        if num_coords == 4:
            return abs(np.linalg.det(coord[:-1] - coord[1:])) / 6.0
        elif num_coords == 8:
            b = coord[np.array([[0, 1, 3, 4], [7, 3, 6, 4], [4, 5, 1, 6],
                                [1, 6, 3, 4], [2, 6, 3, 1]])]
            return np.sum(np.abs(np.linalg.det(b[:, :-1] - b[:, 1:]))) / 6.0
        else:
            return None

    def ve_center(self, ve):
        """Finds the point at the center of any tetrahedral or hexahedral mesh
        volume element.

        Parameters
        ----------
        ve : iMesh entity handle
           Any mesh volume element.

        Returns
        -------
        center : tuple
           The (x, y, z) coordinates of the center of the mesh volume element.
        """
        coords = self.mesh.get_coords(self.mesh.get_connectivity(ve)).reshape(-1,3)
        center = tuple([np.mean(coords[:, x]) for x in range(3)])
        return center

    # Structured methods:
    def structured_get_vertex(self, i, j, k):
        """Return the handle for (i,j,k)'th vertex in the mesh"""
        self._structured_check()
        n = _structured_find_idx(self.vertex_dims, (i, j, k))
        return _structured_step_iter(
            meshset_iterate(self.mesh, self.structured_set, entity_type = types.MBVERTEX), n)

    def structured_get_hex(self, i, j, k):
        """Return the handle for the (i,j,k)'th hexahedron in the mesh"""
        self._structured_check()
        n = _structured_find_idx(self.dims, (i, j, k))
        return _structured_step_iter(
            meshset_iterate(self.mesh, self.structured_set, types.MBHEX, 3), n)

    def structured_hex_volume(self, i, j, k):
        """Return the volume of the (i,j,k)'th hexahedron in the mesh"""
        self._structured_check()
        v = list(self.structured_iterate_vertex(x=[i, i + 1],
                                                y=[j, j + 1],
                                                z=[k, k + 1]))
        handle = self.structured_get_hex(i,j,k)
        h = self.mesh.get_connectivity([handle,])
        coord = self.mesh.get_coords(list(h))
        coord = coord.reshape(8,3)
        # assumes a "well-behaved" hex element
        dx = max(coord[:,0]) - min(coord[:,0])
        dy = max(coord[:,1]) - min(coord[:,1])
        dz = max(coord[:,2]) - min(coord[:,2])
        return dx * dy * dz

    def structured_iterate_hex(self, order="zyx", **kw):
        """Get an iterator over the hexahedra of the mesh

        The order argument specifies the iteration order.  It must be a string
        of 1-3 letters from the set (x,y,z).  The rightmost letter is the axis
        along which the iteration will advance the most quickly.  Thus "zyx" --
        x coordinates changing fastest, z coordinates changing least fast-- is
        the default, and is identical to the order that would be given by the
        structured_set.iterate() function.

        When a dimension is absent from the order, iteration will proceed over
        only the column in the mesh that has the lowest corresonding (i/j/k)
        coordinate.  Thus, with order "xy," iteration proceeds over the i/j
        plane of the structured mesh with the smallest k coordinate.

        Specific slices can be specified with keyword arguments:

        Keyword args::

          x: specify one or more i-coordinates to iterate over.
          y: specify one or more j-coordinates to iterate over.
          z: specify one or more k-coordinates to iterate over.

        Examples::

          structured_iterate_hex(): equivalent to iMesh iterator over hexes
                                    in mesh
          structured_iterate_hex("xyz"): iterate over entire mesh, with
                                         k-coordinates changing fastest,
                                         i-coordinates least fast.
          structured_iterate_hex("yz", x=3): Iterate over the j-k plane of the
                                             mesh whose i-coordinate is 3, with
                                             k values changing fastest.
          structured_iterate_hex("z"): Iterate over k-coordinates, with
                                       i=dims.imin and j=dims.jmin
          structured_iterate_hex("yxz", y=(3,4)): Iterate over all hexes with
                                        j-coordinate = 3 or 4.  k-coordinate
                                        values change fastest, j-values least
                                        fast.
        """
        self._structured_check()

        # special case: zyx order is the standard pytaps iteration order,
        # so we can save time by simply returning a pytaps iterator
        # if no kwargs were specified
        if order == "zyx" and not kw:
            return meshset_iterate(self.mesh, self.structured_set, entity_type = types.MBHEX, dim = 3)

        indices, ordmap = _structured_iter_setup(self.dims, order, **kw)
        return _structured_iter(indices, ordmap, self.dims,
                                meshset_iterate(self.mesh,
                                                self.structured_set,
                                                entity_type = types.MBHEX,
                                                dim = 3))

    def structured_iterate_vertex(self, order="zyx", **kw):
        """Get an iterator over the vertices of the mesh

        See structured_iterate_hex() for an explanation of the order argument
        and the available keyword arguments.
        """
        self._structured_check()
        # special case: zyx order without kw is equivalent to pytaps iterator
        if order == "zyx" and not kw:
            return meshset_iterate(self.mesh, self.structured_set, entity_type = types.MBVERTEX)

        indices, ordmap = _structured_iter_setup(self.vertex_dims, order, **kw)
        return _structured_iter(indices, ordmap, self.vertex_dims,
                                meshset_iterate(self.mesh, self.structured_set, entity_type = types.MBVERTEX))

    def structured_iterate_hex_volumes(self, order="zyx", **kw):
        """Get an iterator over the volumes of the mesh hexahedra

        See structured_iterate_hex() for an explanation of the order argument
        and the available keyword arguments.
        """
        self._structured_check()
        indices, _ = _structured_iter_setup(self.dims, order, **kw)
        # Use an inefficient but simple approach: call structured_hex_volume()
        # on each required i,j,k pair.
        # A better implementation would only make one call to getVtxCoords.
        for A in itertools.product(*indices):
            # the ordmap returned from _structured_iter_setup maps to kji/zyx
            # ordering, but we want ijk/xyz ordering, so create the ordmap
            # differently.
            ordmap = [order.find(L) for L in "xyz"]
            ijk = [A[ordmap[x]] for x in range(3)]
            yield self.structured_hex_volume(*ijk)

    def iter_structured_idx(self, order=None):
        """Return an iterater object of volume element indexes (idx) for any
        iteration order. Note that idx is assigned upon instantiation in the
        order of the structured_ordering attribute. This method is meant to be
        used when the order argument is different from structured_ordering.
        When they are the same, the iterator (0, 1, 2, ... N-1) is returned.

        Parameters
        ----------
        order : str, optional
            The requested iteration order (e.g. 'zyx').
        """
        self._structured_check()
        if not order:
            order = self.structured_ordering

        ves = self.structured_iterate_hex(order)
        tag = self.mesh.tag_get_handle('idx')
        for val in self.mesh.tag_get_data(tag, ves, flat = True):
            yield val

    def structured_get_divisions(self, dim):
        """Get the mesh divisions on a given dimension

        Given a dimension "x", "y", or "z", return a list of the mesh vertices
        along that dimension.
        """
        self._structured_check()

        if len(dim) == 1 and dim in "xyz":
            idx = "xyz".find(dim)
            return [self.mesh.get_coords(v)[idx] for v in self.structured_iterate_vertex(dim)]

        else:
            raise MeshError("Invalid dimension: {0}".format(str(dim)))

    def _structured_check(self):
        if not self.structured:
            raise MeshError("Structured mesh methods cannot be called from "\
                            "unstructured mesh instances.")

    def save(self, filename):
        self.write_hdf5(filename)

    def write_hdf5(self, filename):
        """Writes the mesh to an hdf5 file."""
        self.mesh.write_file(filename)
        if self.mats is not None:
            self.mats.write_hdf5(filename)

    def cell_fracs_to_mats(self, cell_fracs, cell_mats):
        """This function uses the output from dagmc.discretize_geom() and
        a mapping of geometry cells to Materials to assign materials
        to each mesh volume element.

        Parameters
        ----------
        cell_fracs : structured array
            The output from dagmc.discretize_geom(). A sorted, one dimensional
            array, each entry containing the following fields:

                :idx: int
                    The volume element index.
                :cell: int
                    The geometry cell number.
                :vol_frac: float
                    The volume fraction of the cell withing the mesh ve.
                :rel_error: float
                    The relative error associated with the volume fraction.

            The array must be sorted with respect to both idx and cell, with
            cell changing fastest.
        cell_mats : dict
            Maps geometry cell numbers to Material objects that represent what
            material each cell is made of.

        """
        for i in range(len(self)):
            mat_col = {}  # Collection of materials in the ith ve.
            for row in cell_fracs[cell_fracs['idx'] == i]:
                mat_col[cell_mats[row['cell']]] = row['vol_frac']

            mixed = MultiMaterial(mat_col)
            self.mats[i] = mixed.mix_by_volume()

    def tag_cell_fracs(self, cell_fracs):
        """This function uses the output from dagmc.discretize_geom() and
        a mapping of geometry cells to set the cell_fracs_tag.

        Parameters
        ----------
        cell_fracs : structured array
            The output from dagmc.discretize_geom(). A sorted, one dimensional
            array, each entry containing the following fields:

                :idx: int
                    The volume element index.
                :cell: int
                    The geometry cell number.
                :vol_frac: float
                    The volume fraction of the cell withing the mesh ve.
                :rel_error: float
                    The relative error associated with the volume fraction.

            The array must be sorted with respect to both idx and cell, with
            cell changing fastest.

        """

        num_vol_elements = len(self)
        # Find the maximum cell number in a voxel
        max_num_cells = -1
        for i in range(num_vol_elements):
            max_num_cells = max(max_num_cells,
                                len(cell_fracs[cell_fracs['idx'] == i]))

        # create tag frame with default value
        cell_largest_frac_number = [-1] * num_vol_elements
        cell_largest_frac = [0.0] * num_vol_elements
        voxel_cell_number = np.empty(shape=(num_vol_elements, max_num_cells),
                                     dtype=int)
        voxel_cell_fracs = np.empty(shape=(num_vol_elements, max_num_cells),
                                    dtype=float)
        voxel_cell_number.fill(-1)
        voxel_cell_fracs.fill(0.0)

        # set the data
        for i in range(num_vol_elements):
            for (cell, row) in enumerate(cell_fracs[cell_fracs['idx'] == i]):
                voxel_cell_number[i, cell] = row['cell']
                voxel_cell_fracs[i, cell] = row['vol_frac']
            # cell_largest_frac_tag
            cell_largest_frac[i] = max(voxel_cell_fracs[i, :])
            largest_index = \
                    list(voxel_cell_fracs[i, :]).index(cell_largest_frac[i])
            cell_largest_frac_number[i] = \
                    int(voxel_cell_number[i, largest_index])

        # create the tags
        self.tag(name='cell_number', value=voxel_cell_number,
                 doc='cell numbers of the voxel, -1 used to fill vacancy',
                 tagtype=IMeshTag, size=max_num_cells, dtype=int)
        self.tag(name='cell_fracs', value=voxel_cell_fracs,
                 tagtype=IMeshTag, doc='volume fractions of each cell in the '
                                       'voxel, 0.0 used to fill vacancy',
                 size=max_num_cells, dtype=float)
        self.tag(name='cell_largest_frac_number',
                 value=cell_largest_frac_number, tagtype=IMeshTag,
                 doc='cell number of the cell with largest volume fraction in '
                     'the voxel', size=1, dtype=int)
        self.tag(name='cell_largest_frac', value=cell_largest_frac,
                 tagtype=IMeshTag, doc='cell fraction of the cell with largest'
                                       'cell volume fraction',
                 size=1, dtype=float)

class StatMesh(Mesh):
    def __init__(self, mesh=None, structured=False,
                 structured_coords=None, structured_set=None, mats=()):

        super(StatMesh, self).__init__(mesh=mesh,
                                       structured=structured,
                                       structured_coords=structured_coords,
                                       structured_set=structured_set, mats=mats)

    def _do_op(self, other, tags, op, in_place=True):
        """Private function to do mesh +, -, *, /. Called by operater
        overloading functions.
        """
        # Exclude error tags because result and error tags are treated
        # simultaneously so there is not need to include both in the tag
        # list to iterate through.
        error_suffix = "_rel_error"

        tags = set(tag for tag in tags if not tag.endswith(error_suffix))

        if in_place:
            mesh_1 = self
        else:
            mesh_1 = copy.copy(self)

        for tag in tags:
            for ve_1, ve_2 in \
                zip(zip(iter(meshset_iterate(mesh_1.mesh, mesh_1.structured_set, types.MBMAXTYPE, dim = 3))),
                    zip(iter(meshset_iterate(other.mesh,  other.structured_set,  types.MBMAXTYPE, dim = 3)))):

                mesh_1_err_tag = mesh_1.mesh.tag_get_handle(tag + error_suffix)
                other_err_tag  =  other.mesh.tag_get_handle(tag +  error_suffix)
                mesh_1_tag = mesh_1.mesh.tag_get_handle(tag)
                other_tag = other.mesh.tag_get_handle(tag)

                mesh_1_val = mesh_1.mesh.tag_get_data(mesh_1_tag, ve_1, flat = True)[0]
                other_val  =  other.mesh.tag_get_data(other_tag,  ve_2, flat = True)[0]
                mesh_1_err = mesh_1.mesh.tag_get_data(mesh_1_err_tag, ve_1, flat = True)[0]
                other_err  =  other.mesh.tag_get_data(other_err_tag,  ve_2, flat = True)[0]

                new_err_val = err__ops[op](mesh_1_val, other_val, mesh_1_err, other_err)
                mesh_1.mesh.tag_set_data(mesh_1_err_tag, ve_1, new_err_val)

                new_val = _ops[op](mesh_1_val, other_val)
                mesh_1.mesh.tag_set_data(mesh_1_tag, ve_1, new_val)

        return mesh_1

######################################################
# private helper functions for structured mesh methods
######################################################

def _structured_find_idx(dims, ijk):
    """Helper method fo structured_get_vertex and structured_get_hex.

    For tuple (i,j,k), return the number N in the appropriate iterator.
    """
    dim0 = [0] * 3
    for i in xrange(0, 3):
        if (dims[i] > ijk[i] or dims[i + 3] <= ijk[i]):
            raise MeshError(str(ijk) + " is out of bounds")
        dim0[i] = ijk[i] - dims[i]
    i0, j0, k0 = dim0
    n = (((dims[4] - dims[1]) * (dims[3] - dims[0]) * k0) +
         ((dims[3] - dims[0]) * j0) +
         i0)
    return n


def _structured_step_iter(it, n):
    """Helper method for structured_get_vertex and structured_get_hex

    Return the nth item in the iterator.
    """
    it.step(n)
    r = it.next()
    it.reset()
    return r


def _structured_iter_setup(dims, order, **kw):
    """Setup helper function for StrMesh iterator functions

    Given dims and the arguments to the iterator function, return
    a list of three lists, each being a set of desired coordinates,
    with fastest-changing coordinate in the last column), and the
    ordmap used by _structured_iter to reorder each coodinate to (i,j,k).
    """
    # a valid order has the letters "x", "y", and "z"
    # in any order without duplicates
    if not (len(order) <= 3 and
            len(set(order)) == len(order) and
            all([a in "xyz" for a in order])):
        raise MeshError("Invalid iteration order: " + str(order))

    # process kw for validity
    spec = {}
    for idx, d in enumerate("xyz"):
        if d in kw:
            spec[d] = kw[d]
            if not isinstance(spec[d], Iterable):
                spec[d] = [spec[d]]
            if not all(x in range(dims[idx], dims[idx + 3])
                       for x in spec[d]):
                raise MeshError("Invalid iterator kwarg: "
                                "{0}={1}".format(d, spec[d]))
            if d not in order and len(spec[d]) > 1:
                raise MeshError("Cannot iterate over" + str(spec[d]) +
                                "without a proper iteration order")
        if d not in order:
            order = d + order
            spec[d] = spec.get(d, [dims[idx]])

    # get indices and ordmap
    indices = []
    for L in order:
        idx = "xyz".find(L)
        indices.append(spec.get(L, xrange(dims[idx], dims[idx + 3])))

    ordmap = ["zyx".find(L) for L in order]
    return indices, ordmap


def _structured_iter(indices, ordmap, dims, it):
    """Iterate over the indices lists, yielding _structured_step_iter(it) for
    each.
    """
    d = [0, 0, 1]
    d[1] = (dims[3] - dims[0])
    d[0] = (dims[4] - dims[1]) * d[1]
    mins = [dims[2], dims[1], dims[0]]
    offsets = ([(a - mins[ordmap[x]]) * d[ordmap[x]]
                for a in indices[x]]
               for x in range(3))
    for ioff, joff, koff in itertools.product(*offsets):
        yield _structured_step_iter(it, (ioff + joff + koff))


if HAVE_PYTAPS:
    def mesh_iterate(mesh, mesh_type = 3,
                     topo_type = types.MBMAXTYPE):
        return meshset_iterate(mesh, 0, topo_type, mesh_type, recursive = True)



def meshset_iterate(pymb, meshset = 0, entity_type = types.MBMAXTYPE, dim = -1, arr_size = 1, recursive = False):

    return MeshSetIterator(pymb, meshset, entity_type, dim, arr_size, recursive)

class MeshSetIterator(object):

    def __init__(self, inst, meshset, entity_type, dim = -1, arr_size = 1, recursive = False):
        self.pymb = inst
        self.meshset = meshset
        self.ent_type = entity_type
        self.dimension = dim
        self.arr_size = arr_size
        self.recur = recursive
        self.reset()

    def reset(self):

        # if a specific dimension is requested, filter get only that dimension
        if(self.ent_type != types.MBMAXTYPE):
            ents = self.pymb.get_entities_by_type(self.meshset, self.ent_type, self.recur)
        # if a specific type is requested, return only that type
        elif(self.dimension != -1):
            ents = self.pymb.get_entities_by_dimension(self.meshset, self.dimension, self.recur)
        # otherwise return everything
        else:
            ents = self.pymb.get_entities_by_handle(self.meshset, self.recur)

        self.pos = 0
        self.size = len(ents)
        self.entities = ents

    def __iter__(self):
        for i in range(0, self.size):
            yield self.entities[i]

    def next(self):
        if self.pos >= self.size:
            raise StopIteration
        else:
            return self.entities[self.pos]
            self.pos += 1

    def step(self, num_steps):
        self.pos += num_steps
        at_end = False
        if self.pos >= self.size:
            self.pos = self.size -1
            at_end = True
        return at_end
