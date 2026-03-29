"""Data loader protocols and concrete helpers."""

from __future__ import annotations

from typing import Any, Hashable, Protocol, Sequence, TypeVar, cast, runtime_checkable

from gepa.core.adapter import DataInst


class ComparableHashable(Hashable, Protocol):
    """Protocol requiring hashing and rich comparison support."""

    def __lt__(self, other: Any, /) -> bool: ...

    def __gt__(self, other: Any, /) -> bool: ...

    def __le__(self, other: Any, /) -> bool: ...

    def __ge__(self, other: Any, /) -> bool: ...


DataId = TypeVar("DataId", bound=ComparableHashable)
""" Generic for the identifier for data examples """


@runtime_checkable
class DataLoader(Protocol[DataId, DataInst]):
    """Minimal interface for retrieving validation examples keyed by opaque ids."""

    def all_ids(self) -> Sequence[DataId]:
        """Return the ordered universe of ids currently available. This may change over time."""
        ...

    def fetch(self, ids: Sequence[DataId]) -> list[DataInst]:
        """Materialise the payloads corresponding to `ids`, preserving order."""
        ...

    def __len__(self) -> int:
        """Return current number of items in the loader."""
        ...


class MutableDataLoader(DataLoader[DataId, DataInst], Protocol):
    """A data loader that can be mutated."""

    def add_items(self, items: list[DataInst]) -> None:
        """Add items to the loader."""


class ListDataLoader(MutableDataLoader[int, DataInst]):
    """In-memory reference implementation backed by a list.

    ``offset`` shifts all IDs by a fixed amount, which is necessary when multiple
    loaders share the same evaluation cache.  Without an offset, train ID 0 and val
    ID 0 would collide in the cache even though they refer to different examples.
    Pass ``offset=len(trainset)`` when constructing the valset loader to guarantee
    disjoint ID spaces.
    """

    def __init__(self, items: Sequence[DataInst], offset: int = 0):
        self.items = list(items)
        self.offset = offset

    def all_ids(self) -> Sequence[int]:
        return list(range(self.offset, self.offset + len(self.items)))

    def fetch(self, ids: Sequence[int]) -> list[DataInst]:
        return [self.items[data_id - self.offset] for data_id in ids]

    def __len__(self) -> int:
        return len(self.items)

    def add_items(self, items: Sequence[DataInst]) -> None:
        self.items.extend(items)


def ensure_loader(data_or_loader: Sequence[DataInst] | DataLoader[DataId, DataInst]) -> DataLoader[DataId, DataInst]:
    if isinstance(data_or_loader, DataLoader):
        return data_or_loader
    if isinstance(data_or_loader, Sequence):
        return cast(DataLoader[DataId, DataInst], ListDataLoader(data_or_loader))
    raise TypeError(f"Unable to cast to a DataLoader type: {type(data_or_loader)}")
