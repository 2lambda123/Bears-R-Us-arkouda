from typeguard import typechecked
from typing import List, Optional, Tuple, Union, Iterable
from arkouda.pdarrayclass import pdarray, argmaxk, attach_pdarray
from arkouda.pdarraycreation import arange, array, zeros
from arkouda.pdarraysetops import argsort, concatenate, in1d
from arkouda.index import Index
from arkouda.groupbyclass import GroupBy, groupable_element_type
from arkouda.dtypes import int64, float64
from arkouda.numeric import value_counts, cast as akcast
from arkouda.util import get_callback
from arkouda.util import convert_if_categorical, register
from arkouda.alignment import lookup
from arkouda.categorical import Categorical
from arkouda.strings import Strings
from arkouda.accessor import CachedAccessor, DatetimeAccessor, StringAccessor

from pandas._config import get_option # type: ignore
import pandas as pd  # type: ignore
import numpy as np  # type: ignore
from warnings import warn

__all__ = [
    "Series",
    ]

import operator


def natural_binary_operators(cls):
    for name, op in {
        '__add__': operator.add,
        '__sub__': operator.sub,
        '__mul__': operator.mul,
        '__truediv__': operator.truediv,
        '__floordiv__': operator.floordiv,
        '__and__': operator.and_,
        '__or__': operator.or_,
        '__xor__': operator.xor,
        '__eq__': operator.eq,
        '__ge__': operator.ge,
        '__gt__': operator.gt,
        '__le__': operator.le,
        '__lshift__': operator.lshift,
        '__lt__': operator.lt,
        '__mod__': operator.mod,
        '__ne__': operator.ne,
        '__rshift__': operator.rshift,
        '__pow__': operator.pow,
    }.items():
        setattr(cls, name, cls._make_binop(op))

    return cls


def unary_operators(cls):
    for name, op in {
        '__invert__': operator.invert,
        '__neg__': operator.neg,
    }.items():
        setattr(cls, name, cls._make_unaryop(op))

    return cls


def aggregation_operators(cls):
    for name in ['max', 'min', 'mean', 'sum', 'std', 'var', 'argmax', 'argmin', 'prod']:
        setattr(cls,name, cls._make_aggop(name))
    return cls


@unary_operators
@aggregation_operators
@natural_binary_operators
class Series:
    """
    One-dimensional arkouda array with axis labels.

    Parameters
    ----------
    index : pdarray, Strings
        an array of indices associated with the data array.
        If empty, it will default to a range of ints whose size match the size of the data.
        optional
    data : Tuple, List, groupable_element_type
        a 1D array. Must not be None if ar_tuple is not provided.

    Raises
    ------
    TypeError
        Raised if index is not a pdarray or Strings object
        Raised if data is not a pdarray, Strings, or Categorical object
    ValueError
        Raised if the index size does not match data size

    Notes
    -----
    The Series class accepts either positional arguments or keyword arguments.
    If entering positional arguments,
        2 arguments entered:
            argument 1 - index
            argument 2 - data
        1 argument entered:
            argument 1 - data
    If entering 1 positional argument, it is assumed that this is the data argument.
    If entering keywords,
        'data' (see Parameters)
        'index' (optional) must match size of 'data'
    """

    @typechecked
    def __init__(self, data: Union[Tuple, List, groupable_element_type],
                 index: Optional[Union[pdarray, Strings]] = None):
        # TODO: Allow index to be an Index when index.py is updated
        if isinstance(data, (tuple, list)) and len(data) == 2:
            # handles the previous `ar_tuple` case
            if not isinstance(data[0], (pdarray, Strings)):
                raise TypeError("indices must be a pdarray or Strings")
            if not isinstance(data[1], (pdarray, Strings, Categorical)):
                raise TypeError("values must be a pdarray, Strings, or Categorical")
            self.values = data[1]
            self.index = Index.factory(data[0])
        else:
            # When only 1 positional argument it will be treated as data and not index
            self.values = array(data) if not isinstance(data, (Strings, Categorical)) else data
            self.index = Index.factory(index) if index is not None else Index(arange(self.values.size))

        if self.index.size != self.values.size:
            raise ValueError("Index size does not match data size")
        self.size = self.index.size

    def __len__(self):
        return self.values.size

    def __repr__(self):
        """
        Return ascii-formatted version of the series.
        """

        if len(self) == 0:
            return 'Series([ -- ][ 0 values : 0 B])'

        maxrows = pd.get_option('display.max_rows')
        if len(self) <= maxrows:
            prt = self.to_pandas()
            length_str = ""
        else:
            prt = pd.concat([self.head(maxrows // 2 + 2).to_pandas(),
                             self.tail(maxrows // 2).to_pandas()])
            length_str = "\nLength {}".format(len(self))
        return prt.to_string(
            dtype=prt.dtype,
            min_rows=get_option("display.min_rows"),
            max_rows=maxrows,
            length=False,
        ) + length_str

    def __getitem__(self, key):
        if type(key) == Series:
            # @TODO align the series indexes
            key = key.values
        return Series((self.index[key], self.values[key]))

    dt = CachedAccessor("dt", DatetimeAccessor)
    str = CachedAccessor("str", StringAccessor)
    # cat = CachedAccessor("cat", CategoricalAccessor)

    @property
    def shape(self):
        # mimic the pandas return of series shape property
        return (self.values.size,)

    def isin(self, lst):
        """Find series elements whose values are in the specified list

        Input
        -----
        Either a python list or an arkouda array.

        Returns
        -------
        Arkouda boolean which is true for elements that are in the list and false otherwise.
        """
        if isinstance(lst, list):
            lst = array(lst)

        boolean = in1d(self.values, lst)
        return Series(data=boolean, index=self.index)

    def locate(self, key):
        """Lookup values by index label

        The input can be a scalar, a list of scalers, or a list of lists (if the series has a MultiIndex).
        As a special case, if a Series is used as the key, the series labels are preserved with its values
        use as the key.

        Keys will be turned into arkouda arrays as needed.

        Returns
        -------

        A Series containing the values corresponding to the key.
        """
        t = type(key)
        if isinstance(key, Series):
            # special case, keep the index values of the Series, and lookup the values
            labels = key.index
            key = key.values
            v = lookup(self.index.index, self.values, key)
            return Series((labels, v))
        elif isinstance(key, pdarray):
            idx = self.index.lookup(key)
        elif t == list or t == tuple:
            key0 = key[0]
            if isinstance(key0, list) or isinstance(key0, tuple):
                # nested list. check if already arkouda arrays
                if not isinstance(key0[0], pdarray):
                    # convert list of lists to list of pdarrays
                    key = [array(a) for a in np.array(key).T.copy()]

            elif not isinstance(key0, pdarray):
                # a list of scalers, convert into arkouda array
                key = array(key)
            # else already list if arkouda array, use as is
            idx = self.index.lookup(key)
        else:
            # scalar value
            idx = self.index == key
        return Series(index=self.index.index[idx], data=self.values[idx])

    @classmethod
    def _make_binop(cls, operator):
        def binop(self, other):
            if type(other) == Series:
                if self.index._check_aligned(other.index):
                    return cls((self.index, operator(self.values, other.values)))
                else:
                    idx = self.index._merge(other.index).index
                    a = lookup(self.index.index, self.values, idx, fillvalue=0)
                    b = lookup(other.index.index, other.values, idx, fillvalue=0)
                    return cls((idx, operator(a, b)))
            else:
                return cls((self.index, operator(self.values, other)))

        return binop

    @classmethod
    def _make_unaryop(cls, operator):
        def unaryop(self):
            return cls((self.index, operator(self.values)))

        return unaryop

    @classmethod
    def _make_aggop(cls, name):
        def aggop(self):
            return getattr(self.values, name)()

        return aggop

    def add(self, b):

        index = self.index.concat(b.index).index

        values = concatenate([self.values, b.values], ordered=False)

        idx, vals = GroupBy(index).sum(values)
        return Series(data=vals, index=idx)

    def topn(self, n=10):
        """ Return the top values of the series

        Parameters
        ----------
        n: Number of values to return

        Returns
        -------
        A new Series with the top values
        """
        k = self.index
        v = self.values

        idx = argmaxk(v, n)
        idx = idx[-1:-n - 1:-1]

        return Series(index=k.index[idx], data=v[idx])

    def sort_index(self, ascending=True):
        """ Sort the series by its index

        Returns
        -------
        A new Series sorted.
        """

        idx = self.index.argsort(ascending=ascending)
        return Series(index=self.index.index[idx], data=self.values[idx])

    def sort_values(self, ascending=True):
        """ Sort the series numerically

        Returns
        -------
        A new Series sorted smallest to largest
        """

        if not ascending:
            if isinstance(self.values, pdarray) and self.values.dtype in (int64, float64):
                # For numeric values, negation reverses sort order
                idx = argsort(-self.values)
            else:
                # For non-numeric values, need the descending arange because reverse slicing not supported
                idx = argsort(self.values)[arange(self.values.size - 1, -1, -1)]
        else:
            idx = argsort(self.values)
        return Series(index=self.index.index[idx], data=self.values[idx])

    def tail(self, n=10):
        """Return the last n values of the series"""

        idx_series = (self.index[-n:])
        return Series(index=idx_series.index, data=self.values[-n:])

    def head(self, n=10):
        """Return the first n values of the series"""

        idx_series = (self.index[0:n])
        return Series(index=idx_series.index, data=self.values[0:n])

    def to_pandas(self):
        """Convert the series to a local PANDAS series"""

        idx = self.index.to_pandas()
        val = convert_if_categorical(self.values)
        return pd.Series(val.to_ndarray(), index=idx)

    def value_counts(self, sort=True):
        """Return a Series containing counts of unique values.

        The resulting object will be in descending order so that the
        first element is the most frequently-occurring element.

        Parameters
        ----------

        sort : Boolean. Whether or not to sort the results.  Default is true.
        """

        dtype = get_callback(self.values)
        idx, vals = value_counts(self.values)
        s = Series(index=idx, data=vals)
        if sort:
            s = s.sort_values(ascending=False)
        s.index.set_dtype(dtype)
        return s

    def diff(self):
        """Diffs consecutive values of the series.

        Returns a new series with the same index and length.  First value is set to NaN.
        """

        values = zeros(len(self), "float64")
        values[1:] = akcast(self.values[1:] - self.values[:-1], "float64")
        values[0] = np.nan

        return Series(data=values, index=self.index)

    def to_dataframe(self, index_labels=None, value_label=None):
        """Converts series to an arkouda data frame

               Parameters
        ----------
        index_labels:  column names(s) to label the index.
        value_label:  column name to label values.
        Returns
        -------
        An arkouda dataframe.
        """
        if value_label is not None:
            value_label = [value_label]

        return Series.concat([self], axis=1, index_labels=index_labels, value_labels=value_label)

    def register(self, label):
        """Register the series with arkouda

                Parameters
                ----------
                label : Arkouda name used for the series

                Returns
                -------
                Numer of keys
                """

        retval = self.index.register(label)
        register(self.values, "{}_value".format(label))
        return retval

    @staticmethod
    def attach(label, nkeys=1):
        """Retrieve a series registered with arkouda

        Parameters
        ----------
        label: name used to register the series
        nkeys: number of keys, if a multi-index was registerd
        """
        v = attach_pdarray(label + "_value")

        if nkeys == 1:
            k = attach_pdarray(label + "_key")
        else:
            k = [attach_pdarray("{}_key_{}".format(label, i)) for i in range(nkeys)]

        return Series((k, v))

    def is_registered(self):
        """
        Checks if all components of the Series object are registered

        Returns
        -------
        bool
            True if all components are registered, false if not

        See Also
        --------
        register, unregister, attach
        """

        # Series contains 2 parts - index and values
        regParts = [self.index.is_registered(), self.values.is_registered()]

        if any(regParts) and not all(regParts):
            warn(f"Series expected {len(regParts)} components to be registered, but only located {sum(regParts)}")

        return all(regParts)

    @staticmethod
    def _all_aligned(array):
        """Is an array of Series indexed aligned?"""

        itor = iter(array)
        a1 = next(itor).index
        for a2 in itor:
            if a1._check_aligned(a2.index) == False:
                return False
        return True

    @staticmethod
    def concat(arrays, axis=0, index_labels=None, value_labels=None):
        """Concatenate in arkouda a list of arkouda Series or grouped arkouda arrays horizontally or vertically.

                If a list of grouped arkouda arrays is passed they are converted to a series. Each grouping is a 2-tuple
                with the first item being the key(s) and the second being the value.

                If horizontal, each series or grouping must have the same length and the same index. The index of the series is
                converted to a column in the dataframe.  If it is a multi-index,each level is converted to a column.

                Parameters
                ----------
                arrays:  The list of series/groupings to concat.
                axis  :  Whether or not to do a verticle (axis=0) or horizontal (axis=1) concatenation
                index_labels:  column names(s) to label the index.
                value_labels:  column names to label values of each series.

                Returns
                -------
                axis=0: an arkouda series.
                axis=1: an arkouda dataframe.
                """
        from arkouda.dataframe import DataFrame

        if len(arrays) == 0:
            raise IndexError("Array length must be non-zero")

        if type(next(iter(arrays))) == tuple:
            arrays = [Series(i) for i in arrays]

        if axis == 1:
            # Horizontal concat
            if value_labels == None:
                value_labels = ["val_{}".format(i) for i in range(len(arrays))]

            if Series._all_aligned(arrays):

                data = next(iter(arrays)).index.to_dict(index_labels)

                for col, label in zip(arrays, value_labels):
                    data[str(label)] = col.values

            else:
                aitor = iter(arrays)
                idx = next(aitor).index
                idx = idx._merge_all([i.index for i in aitor])

                data = idx.to_dict(index_labels)

                for col, label in zip(arrays, value_labels):
                    data[str(label)] = lookup(col.index.index, col.values, idx.index, fillvalue=0)

            retval = DataFrame(data)
        else:
            # Verticle concat
            idx = arrays[0].index
            v = arrays[0].values
            for other in arrays[1:]:
                idx = idx.concat(other.index)
                v = concatenate([v, other.values], ordered=True)
            retval = Series(index=idx.index, data=v)

        return retval

    @staticmethod
    def pdconcat(arrays, axis=0, labels=None):
        """Concatenate a list of arkouda Series or grouped arkouda arrays, returning a PANDAS object.

        If a list of grouped arkouda arrays is passed they are converted to a series. Each grouping is a 2-tuple
        with the first item being the key(s) and the second being the value.

        If horizontal, each series or grouping must have the same length and the same index. The index of the series is
        converted to a column in the dataframe.  If it is a multi-index,each level is converted to a column.

        Parameters
        ----------
        arrays:  The list of series/groupings to concat.
        axis  :  Whether or not to do a verticle (axis=0) or horizontal (axis=1) concatenation
        labels:  names to give the columns of the data frame.

        Returns
        -------
        axis=0: a local PANDAS series
        axis=1: a local PANDAS dataframe
        """
        if len(arrays) == 0:
            raise IndexError("Array length must be non-zero")

        if type(arrays[0]) == tuple:
            arrays = [Series(i) for i in arrays]

        if axis == 1:
            idx = arrays[0].index.to_pandas()

            cols = []
            for col in arrays:
                cols.append(pd.Series(data=col.values.to_ndarray(), index=idx))
            retval = pd.concat(cols, axis=1)
            if labels is not None:
                retval.columns = labels
        else:
            retval = pd.concat([s.to_pandas() for s in arrays])

        return retval