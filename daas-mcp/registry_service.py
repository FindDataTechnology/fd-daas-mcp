"""
Registry query service for daas-mcp.

Query layer over SQLAlchemy models — search, detail, categories, list.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import cast, func, or_, select, String

from models import (
    DaasFunction,
    DaasFunctionColumn,
    DaasSource,
    Category,
    DatasourceForm,
    DatasourceSection,
    DatasourceCollection,
    DatasourceCollectionItem,
    EntityCollection,
    EntityCollectionItem,
    EntityCollectionChange,
    IndicatorRule,
    IndicatorCollection,
    IndicatorCollectionItem,
    IndicatorCollectionChange,
    Rule,
)
from rule_engine import RuleEngine, legacy_shim, script_config_from_path


def _resolve_effective_score(
    item_score: Optional[float], source_default_score: Optional[float]
) -> Optional[float]:
    """Effective score for a collection item: the per-collection override if
    set, else the datasource's default score, else None (unset). Used by
    list_collection and set_collection_item_score so the resolution rule lives
    in one place.
    """
    if item_score is not None:
        return item_score
    return source_default_score


class RegistryService:
    """Query orchestration for DAAS function metadata."""

    def __init__(self, session: Session):
        self._session = session

    def list_sources(self) -> list[dict]:
        sources = self._session.query(DaasSource).order_by(DaasSource.name).all()
        result = []
        for s in sources:
            cnt = (
                self._session.query(func.count(DaasFunction.id))
                .filter(DaasFunction.source_id == s.id)
                .scalar()
            )
            d = s.to_dict()
            d["function_count"] = cnt
            result.append(d)
        return result

    def search_functions(self, query: str, source: Optional[str] = None, limit: int = 20) -> list[dict]:
        q = f"%{query}%"
        q_obj = (
            self._session.query(DaasFunction)
            .join(DaasSource)
            .filter(
                or_(
                    DaasFunction.name.like(q),
                    DaasFunction.category.like(q),
                    DaasFunction.description.like(q),
                )
            )
        )
        if source:
            q_obj = q_obj.filter(DaasSource.name == source)
        results = q_obj.order_by(DaasSource.name, DaasFunction.name).limit(limit).all()
        return [f.to_dict() for f in results]

    def get_function_detail(self, name: str) -> Optional[dict]:
        func = self._session.query(DaasFunction).filter(DaasFunction.name == name).first()
        if func is None:
            return None
        return func.to_dict()

    def list_categories(self, source: Optional[str] = None) -> list[dict]:
        q = (
            self._session.query(
                DaasSource.name.label("source_name"),
                DaasFunction.category,
                func.count(DaasFunction.id).label("cnt"),
            )
            .join(DaasFunction.source)
        )
        if source:
            q = q.filter(DaasSource.name == source)
        rows = (
            q.group_by(DaasSource.name, DaasFunction.category)
            .order_by(DaasSource.name, func.count(DaasFunction.id).desc())
            .all()
        )
        return [
            {"source": row.source_name, "category": row.category, "count": row.cnt}
            for row in rows
        ]

    # ════════════════════════════════════════════════════════════
    # Management domain — categories, datasource CRUD, forms/sections,
    # collections, multi-level search (additive to the read API above)
    # ════════════════════════════════════════════════════════════

    # --- Category tree -----------------------------------------------------

    _MAX_DEPTH = 100  # ponytail: cycle guard ceiling; trees are small

    def create_category(
        self,
        name: str,
        label: Optional[str] = None,
        parent_id: Optional[int] = None,
        sort_order: Optional[int] = None,
    ) -> dict:
        if parent_id is not None:
            parent = self._session.get(Category, parent_id)
            if parent is None:
                raise ValueError(f"Parent category id {parent_id} not found")
        cat = Category(name=name, label=label or name, parent_id=parent_id, sort_order=sort_order)
        self._session.add(cat)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return cat.to_dict()

    def _ancestor_ids(self, category_id: int) -> set[int]:
        """Walk parent chain; cycle-safe via visited set + depth cap."""
        ids: set[int] = set()
        cur = category_id
        depth = 0
        while cur is not None and depth < self._MAX_DEPTH:
            if cur in ids:
                break  # cycle
            ids.add(cur)
            cat = self._session.get(Category, cur)
            if cat is None:
                break
            cur = cat.parent_id
            depth += 1
        return ids

    def move_category(self, category_id: int, parent_id: Optional[int]) -> dict:
        cat = self._session.get(Category, category_id)
        if cat is None:
            raise ValueError(f"Category id {category_id} not found")
        if parent_id is not None:
            if parent_id == category_id:
                raise ValueError("A category cannot be its own parent")
            parent = self._session.get(Category, parent_id)
            if parent is None:
                raise ValueError(f"Parent category id {parent_id} not found")
            # Reject moving into own descendant subtree (would create a cycle)
            if parent_id in self._descendant_ids(category_id):
                raise ValueError("Cannot move a category into its own descendant subtree")
        cat.parent_id = parent_id
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return cat.to_dict()

    def _descendant_ids(self, category_id: int) -> set[int]:
        """BFS down the subtree; cycle-safe via visited set + depth cap."""
        result: set[int] = set()
        frontier = [category_id]
        depth = 0
        while frontier and depth < self._MAX_DEPTH:
            nxt: list[int] = []
            for cid in frontier:
                children = (
                    self._session.query(Category.id)
                    .filter(Category.parent_id == cid)
                    .all()
                )
                for (child_id,) in children:
                    if child_id in result:
                        continue
                    result.add(child_id)
                    nxt.append(child_id)
            frontier = nxt
            depth += 1
        return result

    def get_subtree_ids(self, category_id: int) -> list[int]:
        """Return the category + all descendant ids. Cycle-safe."""
        return [category_id, *sorted(self._descendant_ids(category_id))]

    def delete_category(self, category_id: int) -> dict:
        cat = self._session.get(Category, category_id)
        if cat is None:
            raise ValueError(f"Category id {category_id} not found")
        child_count = (
            self._session.query(func.count(Category.id))
            .filter(Category.parent_id == category_id)
            .scalar()
        )
        if child_count:
            raise ValueError(
                f"Category '{cat.name}' has {child_count} child categories; "
                "move or delete them first"
            )
        # Orphan assigned datasources to root level (category_id = NULL)
        self._session.query(DaasSource).filter(DaasSource.category_id == category_id).update(
            {DaasSource.category_id: None}
        )
        self._session.delete(cat)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {"deleted": category_id}

    def get_category_tree(self, root_id: Optional[int] = None) -> list[dict]:
        if root_id is not None:
            roots = [self._session.get(Category, root_id)]
            roots = [r for r in roots if r is not None]
        else:
            roots = (
                self._session.query(Category)
                .filter(Category.parent_id.is_(None))
                .order_by(Category.sort_order.nulls_last(), Category.name)
                .all()
            )
        return [self._category_node(r) for r in roots]

    def _category_node(self, cat: Category) -> dict:
        d = cat.to_dict()
        d["datasource_count"] = (
            self._session.query(func.count(DaasSource.id))
            .filter(DaasSource.category_id == cat.id)
            .scalar()
        )
        children = sorted(
            (c for c in (cat.children or []) if c is not None),
            key=lambda c: (c.sort_order if c.sort_order is not None else 0, c.name),
        )
        d["children"] = [self._category_node(c) for c in children]
        return d

    def _category_path(self, category_id: Optional[int]) -> Optional[list[str]]:
        if category_id is None:
            return None
        path: list[str] = []
        cur = category_id
        depth = 0
        while cur is not None and depth < self._MAX_DEPTH:
            cat = self._session.get(Category, cur)
            if cat is None:
                break
            if cat.name in path:  # cycle guard
                break
            path.append(cat.name)
            cur = cat.parent_id
            depth += 1
        path.reverse()
        return path

    # --- Datasource CRUD ---------------------------------------------------

    def create_datasource(
        self,
        name: str,
        label: str,
        description: Optional[str] = None,
        url: Optional[str] = None,
        config: Optional[dict] = None,
        category_id: Optional[int] = None,
        enabled: bool = True,
        score: Optional[float] = None,
    ) -> dict:
        existing = self._session.query(DaasSource).filter(DaasSource.name == name).first()
        if existing is not None:
            raise ValueError(f"Datasource '{name}' already exists")
        if category_id is not None:
            if self._session.get(Category, category_id) is None:
                raise ValueError(f"Category id {category_id} not found")
        src = DaasSource(
            name=name,
            label=label,
            description=description,
            url=url,
            config=config,
            category_id=category_id,
            enabled=enabled,
            score=score,
        )
        self._session.add(src)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return self._source_detail(src)

    def update_datasource(
        self,
        name: Optional[str] = None,
        datasource_id: Optional[int] = None,
        label: Optional[str] = None,
        description: Optional[str] = None,
        url: Optional[str] = None,
        config: Optional[dict] = None,
        enabled: Optional[bool] = None,
        category_id: Optional[int] = None,
        clear_category: bool = False,
        score: Optional[float] = None,
        clear_score: bool = False,
    ) -> dict:
        src = self._resolve_source(name, datasource_id)
        if src is None:
            raise ValueError("Datasource not found")
        if label is not None:
            src.label = label
        if description is not None:
            src.description = description
        if url is not None:
            src.url = url
        if config is not None:
            src.config = config
        if enabled is not None:
            src.enabled = enabled
        if clear_category:
            src.category_id = None
        elif category_id is not None:
            if self._session.get(Category, category_id) is None:
                raise ValueError(f"Category id {category_id} not found")
            src.category_id = category_id
        if clear_score:
            src.score = None
        elif score is not None:
            src.score = score
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return self._source_detail(src)

    def delete_datasource(
        self, name: Optional[str] = None, datasource_id: Optional[int] = None
    ) -> dict:
        src = self._resolve_source(name, datasource_id)
        if src is None:
            raise ValueError("Datasource not found")
        deleted = src.name
        # FK ON DELETE CASCADE handles forms/sections/collection items
        self._session.delete(src)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {"deleted": deleted}

    def _resolve_source(
        self, name: Optional[str], datasource_id: Optional[int]
    ) -> Optional[DaasSource]:
        if datasource_id is not None:
            return self._session.get(DaasSource, datasource_id)
        if name is not None:
            return self._session.query(DaasSource).filter(DaasSource.name == name).first()
        return None

    def _source_detail(self, src: DaasSource) -> dict:
        d = src.to_dict()
        d["id"] = src.id
        d["category_path"] = self._category_path(src.category_id)
        d["form_count"] = (
            self._session.query(func.count(DatasourceForm.id))
            .filter(DatasourceForm.source_id == src.id)
            .scalar()
        )
        return d

    def _collection_item_detail(self, item: DatasourceCollectionItem) -> dict:
        """Full detail for one collection item: the item dict plus resolved
        source/section names and the effective score (item override if set,
        else the datasource default). Mirrors the per-item shape returned by
        list_collection so add/set return the same structure."""
        src = self._session.get(DaasSource, item.source_id)
        sec = (
            self._session.get(DatasourceSection, item.section_id) if item.section_id else None
        )
        item_score = item.score
        source_default_score = src.score if src else None
        return {
            "item_id": item.id,
            "collection_id": item.collection_id,
            "source_id": item.source_id,
            "source_name": src.name if src else None,
            "section_id": item.section_id,
            "section_name": sec.section_name if sec else None,
            "instruction": sec.instruction if sec else None,
            "sort_order": item.sort_order,
            "item_score": item_score,
            "source_default_score": source_default_score,
            "score": _resolve_effective_score(item_score, source_default_score),
        }

    # --- Forms & sections --------------------------------------------------

    def add_form(
        self,
        source_name: str,
        form_type: str,
        label: Optional[str] = None,
    ) -> dict:
        src = self._resolve_source(source_name, None)
        if src is None:
            raise ValueError(f"Datasource '{source_name}' not found")
        existing = (
            self._session.query(DatasourceForm)
            .filter(DatasourceForm.source_id == src.id, DatasourceForm.form_type == form_type)
            .first()
        )
        if existing is not None:
            raise ValueError(
                f"Form '{form_type}' already exists for datasource '{source_name}'"
            )
        form = DatasourceForm(source_id=src.id, form_type=form_type, label=label)
        self._session.add(form)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return form.to_dict()

    def add_section(
        self,
        form_id: int,
        section_name: str,
        instruction: Optional[str] = None,
        sort_order: Optional[int] = None,
    ) -> dict:
        form = self._session.get(DatasourceForm, form_id)
        if form is None:
            raise ValueError(f"Form id {form_id} not found")
        existing = (
            self._session.query(DatasourceSection)
            .filter(
                DatasourceSection.form_id == form_id,
                DatasourceSection.section_name == section_name,
            )
            .first()
        )
        if existing is not None:
            raise ValueError(
                f"Section '{section_name}' already exists for form id {form_id}"
            )
        sec = DatasourceSection(
            form_id=form_id,
            section_name=section_name,
            instruction=instruction,
            sort_order=sort_order,
        )
        self._session.add(sec)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return sec.to_dict()

    def list_forms(self, source_name: str) -> list[dict]:
        src = self._resolve_source(source_name, None)
        if src is None:
            raise ValueError(f"Datasource '{source_name}' not found")
        forms = (
            self._session.query(DatasourceForm)
            .filter(DatasourceForm.source_id == src.id)
            .order_by(DatasourceForm.form_type)
            .all()
        )
        return [f.to_dict() for f in forms]

    def _resolve_section(
        self, source_id: int, section_name: str
    ) -> Optional[DatasourceSection]:
        return (
            self._session.query(DatasourceSection)
            .join(DatasourceForm)
            .filter(
                DatasourceForm.source_id == source_id,
                DatasourceSection.section_name == section_name,
            )
            .first()
        )

    # --- Collections -------------------------------------------------------

    def create_collection(self, name: str, description: Optional[str] = None) -> dict:
        existing = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == name)
            .first()
        )
        if existing is not None:
            raise ValueError(f"Collection '{name}' already exists")
        coll = DatasourceCollection(name=name, description=description)
        self._session.add(coll)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return coll.to_dict()

    def add_to_collection(
        self,
        collection_name: str,
        source_name: str,
        section_name: Optional[str] = None,
        score: Optional[float] = None,
    ) -> dict:
        coll = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == collection_name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Collection '{collection_name}' not found")
        src = self._resolve_source(source_name, None)
        if src is None:
            raise ValueError(f"Datasource '{source_name}' not found")
        section_id: Optional[int] = None
        if section_name is not None:
            sec = self._resolve_section(src.id, section_name)
            if sec is None:
                raise ValueError(
                    f"Section '{section_name}' not found for datasource '{source_name}'"
                )
            section_id = sec.id
        dup = (
            self._session.query(DatasourceCollectionItem)
            .filter(
                DatasourceCollectionItem.collection_id == coll.id,
                DatasourceCollectionItem.source_id == src.id,
                DatasourceCollectionItem.section_id.is_(section_id)
                if section_id is None
                else DatasourceCollectionItem.section_id == section_id,
            )
            .first()
        )
        if dup is not None:
            raise ValueError("Item already in collection")
        # Append at end: sort_order = max(existing) + 1, or 0 if empty.
        next_order = (
            self._session.query(func.coalesce(func.max(DatasourceCollectionItem.sort_order), -1))
            .filter(DatasourceCollectionItem.collection_id == coll.id)
            .scalar()
        ) + 1
        item = DatasourceCollectionItem(
            collection_id=coll.id,
            source_id=src.id,
            section_id=section_id,
            sort_order=next_order,
            score=score,
        )
        self._session.add(item)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return self._collection_item_detail(item)

    def list_collection(self, collection_name: str) -> dict:
        coll = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == collection_name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Collection '{collection_name}' not found")
        items = (
            self._session.query(DatasourceCollectionItem)
            .filter(DatasourceCollectionItem.collection_id == coll.id)
            .order_by(DatasourceCollectionItem.sort_order, DatasourceCollectionItem.id)
            .all()
        )
        resolved = []
        for it in items:
            src = self._session.get(DaasSource, it.source_id)
            sec = self._session.get(DatasourceSection, it.section_id) if it.section_id else None
            item_score = it.score
            source_default_score = src.score if src else None
            resolved.append(
                {
                    "item_id": it.id,
                    "source_name": src.name if src else None,
                    "section_id": it.section_id,
                    "section_name": sec.section_name if sec else None,
                    "instruction": sec.instruction if sec else None,
                    "sort_order": it.sort_order,
                    "item_score": item_score,
                    "source_default_score": source_default_score,
                    "score": _resolve_effective_score(item_score, source_default_score),
                }
            )
        return {"collection": coll.to_dict(), "items": resolved}

    def remove_from_collection(
        self,
        collection_name: str,
        source_name: str,
        section_name: Optional[str] = None,
    ) -> dict:
        coll = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == collection_name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Collection '{collection_name}' not found")
        src = self._resolve_source(source_name, None)
        if src is None:
            raise ValueError(f"Datasource '{source_name}' not found")
        section_id: Optional[int] = None
        if section_name is not None:
            sec = self._resolve_section(src.id, section_name)
            section_id = sec.id if sec else None
        q = self._session.query(DatasourceCollectionItem).filter(
            DatasourceCollectionItem.collection_id == coll.id,
            DatasourceCollectionItem.source_id == src.id,
        )
        if section_name is not None:
            q = q.filter(DatasourceCollectionItem.section_id == section_id)
        else:
            q = q.filter(DatasourceCollectionItem.section_id.is_(None))
        item = q.first()
        if item is None:
            raise ValueError("Item not found in collection")
        self._session.delete(item)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {"removed": item.id}

    def set_collection_item_score(
        self,
        collection_name: str,
        source_name: str,
        section_name: Optional[str] = None,
        score: Optional[float] = None,
    ) -> dict:
        """Set or clear the per-collection `score` override on an existing
        collection item. `score=None` clears the override so the item falls
        back to the datasource's default score. Resolves the item by
        (collection, source, optional section) exactly like
        remove_from_collection. Returns the updated item detail (including the
        resolved effective score).
        """
        coll = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == collection_name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Collection '{collection_name}' not found")
        src = self._resolve_source(source_name, None)
        if src is None:
            raise ValueError(f"Datasource '{source_name}' not found")
        section_id: Optional[int] = None
        if section_name is not None:
            sec = self._resolve_section(src.id, section_name)
            if sec is None:
                raise ValueError(
                    f"Section '{section_name}' not found for datasource '{source_name}'"
                )
            section_id = sec.id
        q = self._session.query(DatasourceCollectionItem).filter(
            DatasourceCollectionItem.collection_id == coll.id,
            DatasourceCollectionItem.source_id == src.id,
        )
        if section_name is not None:
            q = q.filter(DatasourceCollectionItem.section_id == section_id)
        else:
            q = q.filter(DatasourceCollectionItem.section_id.is_(None))
        item = q.first()
        if item is None:
            raise ValueError("Item not found in collection")
        # `score is None` clears the override (back to inheriting the default).
        item.score = score
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return self._collection_item_detail(item)

    def list_collections(self) -> list[dict]:
        """All collections with item counts. Used by the dashboard picker."""
        colls = (
            self._session.query(DatasourceCollection)
            .order_by(DatasourceCollection.name)
            .all()
        )
        return [c.to_dict() for c in colls]

    def rename_collection(self, old_name: str, new_name: str) -> dict:
        coll = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == old_name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Collection '{old_name}' not found")
        if new_name != old_name:
            clash = (
                self._session.query(DatasourceCollection)
                .filter(DatasourceCollection.name == new_name)
                .first()
            )
            if clash is not None:
                raise ValueError(f"Collection '{new_name}' already exists")
        coll.name = new_name
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return coll.to_dict()

    def update_collection(
        self,
        name: str,
        new_name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """Partially update a collection's name and/or description.

        At least one of `new_name` / `description` must be provided; omitted
        fields are left unchanged. When `new_name` is set and differs from the
        current name, the new name must be unique (clash raises ValueError).
        Raises ValueError if the collection is not found or no fields were
        provided. Preserves all `datasource_collection_items` rows (they
        reference by collection_id, not by name). `updated_at` is bumped by
        the model's `onupdate`.
        """
        if new_name is None and description is None:
            raise ValueError("At least one of new_name or description is required")
        coll = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Collection '{name}' not found")
        if new_name is not None and new_name != name:
            clash = (
                self._session.query(DatasourceCollection)
                .filter(DatasourceCollection.name == new_name)
                .first()
            )
            if clash is not None:
                raise ValueError(f"Collection '{new_name}' already exists")
            coll.name = new_name
        # An explicit empty string clears the description; None leaves it unchanged.
        if description is not None:
            coll.description = description
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return coll.to_dict()

    def delete_collection(self, name: str) -> dict:
        coll = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Collection '{name}' not found")
        # FK ON DELETE CASCADE handles datasource_collection_items.
        self._session.delete(coll)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {"deleted": name}

    def reorder_collection_items(
        self, collection_name: str, ordered_item_ids: list[int]
    ) -> dict:
        """Rewrite sort_order for items in `collection_name` to match the
        given ordered list. Rejects the whole reorder if any id isn't in
        the collection or if the id set doesn't exactly match.
        """
        coll = (
            self._session.query(DatasourceCollection)
            .filter(DatasourceCollection.name == collection_name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Collection '{collection_name}' not found")
        items = (
            self._session.query(DatasourceCollectionItem)
            .filter(DatasourceCollectionItem.collection_id == coll.id)
            .all()
        )
        existing_ids = {it.id for it in items}
        requested_ids = list(ordered_item_ids)
        unknown = set(requested_ids) - existing_ids
        if unknown:
            raise ValueError(
                f"Items {sorted(unknown)} are not in collection '{collection_name}'"
            )
        if set(requested_ids) != existing_ids:
            missing = existing_ids - set(requested_ids)
            raise ValueError(
                f"Reorder must include every collection item; missing: {sorted(missing)}"
            )
        by_id = {it.id: it for it in items}
        for new_order, item_id in enumerate(requested_ids):
            by_id[item_id].sort_order = new_order
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {"collection": collection_name, "order": requested_ids}

    # --- Multi-level search ------------------------------------------------

    def search_datasources(
        self,
        category_id: Optional[int] = None,
        include_subtree: bool = True,
        source_name: Optional[str] = None,
        form: Optional[str] = None,
        section: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        limit = min(max(limit, 1), 500)
        expand = (form is not None) or (section is not None) or (query is not None)

        q = self._session.query(DaasSource)
        if category_id is not None:
            cat_ids = self.get_subtree_ids(category_id) if include_subtree else [category_id]
            q = q.filter(DaasSource.category_id.in_(cat_ids))
        if source_name is not None:
            q = q.filter(DaasSource.name == source_name)
        if query is not None:
            like = f"%{query}%"
            # Free-text across source label/description/name OR any form label
            # OR any section name/instruction. Sources matching only via a
            # form/section must still be included, so expand the source set
            # with sources that own a matching form/section.
            like_q = f"%{query}%"
            matching_source_ids_via_form = (
                select(DatasourceForm.source_id)
                .where(DatasourceForm.label.like(like_q))
                .distinct()
            )
            matching_source_ids_via_section = (
                select(DatasourceForm.source_id)
                .join(DatasourceSection, DatasourceSection.form_id == DatasourceForm.id)
                .where(
                    or_(
                        DatasourceSection.section_name.like(like_q),
                        DatasourceSection.instruction.like(like_q),
                    )
                )
                .distinct()
            )
            q = q.filter(
                or_(
                    DaasSource.label.like(like),
                    DaasSource.description.like(like),
                    DaasSource.name.like(like),
                    DaasSource.id.in_(matching_source_ids_via_form),
                    DaasSource.id.in_(matching_source_ids_via_section),
                )
            )
        sources = q.order_by(DaasSource.name).limit(limit).all()

        results = []
        for src in sources:
            if not expand:
                d = self._source_detail(src)
                results.append(d)
                continue
            # Expand forms/sections, applying form/section/query filters
            matched_forms = self._matching_forms(src, form, section, query)
            if form is not None or section is not None:
                # Drill filters: only keep sources that actually match
                if not matched_forms and (form is not None or section is not None):
                    continue
            d = self._source_detail(src)
            d["forms"] = matched_forms if matched_forms else []
            results.append(d)
        return results

    def _matching_forms(
        self,
        src: DaasSource,
        form: Optional[str],
        section: Optional[str],
        query: Optional[str],
    ) -> list[dict]:
        q = self._session.query(DatasourceForm).filter(DatasourceForm.source_id == src.id)
        if form is not None:
            q = q.filter(DatasourceForm.form_type == form)
        forms = q.order_by(DatasourceForm.form_type).all()
        out = []
        like = f"%{query}%" if query else None
        for f in forms:
            secs_q = self._session.query(DatasourceSection).filter(
                DatasourceSection.form_id == f.id
            )
            if section is not None:
                secs_q = secs_q.filter(DatasourceSection.section_name.like(f"%{section}%"))
            secs = secs_q.order_by(DatasourceSection.sort_order.nulls_last()).all()
            # Free-text query: include form if label matches, or any section matches
            form_label_match = like and f.label and like.lower() in f.label.lower()
            if like and not form_label_match:
                secs = [
                    s
                    for s in secs
                    if (s.section_name and like.lower() in s.section_name.lower())
                    or (s.instruction and like.lower() in s.instruction.lower())
                ]
            if section is not None and not secs and form is None:
                continue
            fd = {
                "id": f.id,
                "form_type": f.form_type,
                "label": f.label,
                "sections": [s.to_dict() for s in secs],
            }
            out.append(fd)
        return out

    # ════════════════════════════════════════════════════════════
    # Entity domain — stocks + countries, linked to daas `sources`
    # ════════════════════════════════════════════════════════════

    def _proxy_get_entity_via_gateway(
        self, entity_type: str, code: str
    ) -> Optional[dict]:
        """Fetch one entity from the fd-open-data-mcp master by natural key.

        Returns a daas-shaped dict (normalized from the gateway's
        ``{id, entity_type, code, name_en, name_zh, metadata}``), or ``None``
        if the gateway is unavailable or the entity is not found there
        (caller falls back to the local ``entities`` table).
        """
        sync_call = self._load_gateway_sync()
        if sync_call is None:
            return None
        try:
            resp = sync_call(
                "fd-open-data-mcp",
                "get_entity",
                json.dumps({"entity_type": entity_type, "code": code}),
            )
        except Exception:
            return None
        if not isinstance(resp, dict) or "error" in resp:
            return None
        data = resp.get("result")
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return self._normalize_gateway_entity(data)

    def _proxy_list_entities_via_gateway(
        self, entity_type: str, limit: int, offset: int
    ) -> Optional[list[dict]]:
        """List entities of a type from the fd-open-data-mcp master.

        Returns a list of daas-shaped dicts, or ``None`` if the gateway is
        unavailable (caller falls back to the local ``entities`` table).
        """
        sync_call = self._load_gateway_sync()
        if sync_call is None:
            return None
        try:
            resp = sync_call(
                "fd-open-data-mcp",
                "list_entities",
                json.dumps(
                    {"entity_type": entity_type, "limit": limit, "offset": offset}
                ),
            )
        except Exception:
            return None
        if not isinstance(resp, dict) or "error" in resp:
            return None
        data = resp.get("result")
        if not isinstance(data, list):
            return None
        return [self._normalize_gateway_entity(d) for d in data if isinstance(d, dict)]

    @staticmethod
    def _normalize_gateway_entity(data: dict) -> dict:
        """Map a gateway entity dict to the daas entity shape so callers see
        a consistent surface during the migration (gateway is canonical
        post-3.7; local fallback still serves the old shape until then)."""
        meta = data.get("metadata") or {}
        if isinstance(meta, str):
            try:
                import json as _json

                meta = _json.loads(meta) or {}
            except Exception:
                meta = {}
        return {
            "id": data.get("id"),
            "entity_type": data.get("entity_type"),
            "code": data.get("code"),
            "name": data.get("name_en") or data.get("name_zh"),
            "name_en": data.get("name_en"),
            "name_zh": data.get("name_zh"),
            "ticker": meta.get("ticker"),
            "exchange": meta.get("exchange"),
            "country_code": meta.get("country_code"),
            "aliases": meta.get("aliases"),
            "metadata": meta,
        }

    def search_entities(
        self, query: str, entity_type: Optional[str] = None, limit: int = 20
    ) -> list[dict]:
        """Case-insensitive substring match on name / ticker / code / aliases.

        Per D5/3.5 the entity master is fd-open-data-mcp. The gateway has no
        text-search tool, so when ``entity_type`` is given we fetch a batch
        via ``list_entities`` and filter client-side; otherwise (or on any
        gateway issue) we return ``[]`` — the local ``entities`` table was
        dropped in 3.7.
        """
        limit = min(max(limit, 1), 100)
        ql = query.lower()
        if entity_type:
            proxied = self._proxy_list_entities_via_gateway(entity_type, 500, 0)
            if proxied is not None:
                hits = [
                    e
                    for e in proxied
                    if ql in (e.get("name") or "").lower()
                    or ql in (e.get("code") or "").lower()
                    or ql in (e.get("ticker") or "").lower()
                    or ql in str(e.get("aliases") or "").lower()
                ]
                return hits[:limit]
        return []

    def get_entity(
        self,
        entity_id: Optional[int] = None,
        entity_type: Optional[str] = None,
        code: Optional[str] = None,
    ) -> Optional[dict]:
        """Get full detail for one entity.

        Per D5/3.5 the entity master is fd-open-data-mcp. When a natural
        key (entity_type, code) is given, proxy via the gateway. When only
        entity_id is given there is no gateway tool that resolves a bare id
        and the local ``entities`` table was dropped in 3.7 — return None.
        """
        if entity_type and code:
            return self._proxy_get_entity_via_gateway(entity_type, code)
        return None

    def list_entities(
        self,
        entity_type: Optional[str] = None,
        exchange: Optional[str] = None,
        country_code: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """List entities filtered by type / exchange / country, paginated.

        Per D5/3.5, the gateway (fd-open-data-mcp) is the entity master.
        Proxy through the gateway when possible; on gateway error (or a
        missing ``entity_type``) return an empty page — the local
        ``entities`` table was dropped in 3.7.
        """
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)

        gateway_result = self._list_entities_via_gateway(
            entity_type=entity_type,
            exchange=exchange,
            country_code=country_code,
            limit=limit,
            offset=offset,
        )
        if gateway_result is not None:
            return gateway_result

        return {
            "entities": [],
            "count": 0,
            "total": 0,
            "offset": offset,
        }

    def _list_entities_via_gateway(
        self,
        entity_type: Optional[str],
        exchange: Optional[str],
        country_code: Optional[str],
        limit: int,
        offset: int,
    ) -> Optional[dict]:
        """Proxy ``list_entities`` through the fd-open-data-mcp gateway.

        The gateway's ``list_entities`` takes ``(entity_type, limit, offset)``
        and returns ``{id, entity_type, code, name_en, name_zh, metadata}``.
        We apply ``exchange``/``country_code`` filters in-memory (the gateway
        doesn't filter on these). Returns ``None`` if the gateway is
        unavailable (caller falls back to local).
        """
        if not entity_type:
            # Gateway requires entity_type; can't proxy without it.
            return None
        sync_call = self._load_gateway_sync()
        if sync_call is None:
            return None
        try:
            resp = sync_call(
                "fd-open-data-mcp",
                "list_entities",
                json.dumps({"entity_type": entity_type, "limit": limit, "offset": offset}),
            )
        except Exception:
            return None
        if not isinstance(resp, dict) or "error" in resp:
            return None
        data = resp.get("result")
        if not isinstance(data, list):
            return None
        # Apply exchange/country_code filters in-memory (gateway doesn't filter).
        filtered = data
        if exchange or country_code:
            filtered = []
            for e in data:
                meta = e.get("metadata") or {}
                if exchange and meta.get("exchange") != exchange:
                    continue
                if country_code and meta.get("country_code") != country_code:
                    continue
                filtered.append(e)
        # Coerce gateway shape to local Entity.to_dict() shape.
        entities = []
        for e in filtered:
            meta = e.get("metadata") or {}
            entities.append({
                "id": e.get("id"),
                "entity_type": e.get("entity_type"),
                "code": e.get("code"),
                "name": e.get("name_en") or e.get("name_zh"),
                "name_en": e.get("name_en"),
                "name_zh": e.get("name_zh"),
                "ticker": meta.get("ticker"),
                "exchange": meta.get("exchange"),
                "country_code": meta.get("country_code"),
                "aliases": meta.get("aliases", []),
                "metadata": meta,
            })
        return {
            "entities": entities,
            "count": len(entities),
            "total": len(entities),  # gateway doesn't return total; approximate
            "offset": offset,
        }

    def get_entity_coverage(
        self,
        entity_id: Optional[int] = None,
        entity_type: Optional[str] = None,
        code: Optional[str] = None,
    ) -> dict:
        """Return the datasources linked to an entity.

        Per D5/3.5, entity identity proxies through fd-open-data-mcp and the
        local ``entity_datasource_links`` table was dropped in 3.7. There is
        no local link data left to aggregate, so resolve the entity via the
        gateway (when a natural key is given) and return empty coverage. The
        identifier-per-source routing data now lives upstream.
        """
        if entity_type and code:
            entity_info = self._proxy_get_entity_via_gateway(entity_type, code)
            if entity_info is None:
                raise ValueError(f"Entity {entity_type}/{code} not found")
            eid = entity_info.get("id")
        elif entity_id is not None:
            # No gateway tool resolves a bare id and there is no local table
            # to look it up in — nothing to return.
            raise ValueError(f"Entity id {entity_id} not resolvable (no local entity master)")
        else:
            raise ValueError("entity_id or (entity_type, code) required")
        return {
            "entity_id": eid,
            "entity": entity_info,
            "datasources": [],
            "count": 0,
        }

    @staticmethod
    def _substitute_identifier(instruction: str, identifier: Optional[str]) -> str:
        """Replace <ask-agent> only in params whose key is an identifier key
        (ticker_or_cik, ticker_or_name, ticker_or_code, ticker, symbol, code).
        Other <ask-agent> params stay — the agent must still supply them."""
        if not instruction or not identifier:
            return instruction
        out: list[str] = []
        for token in instruction.split():
            if token.startswith("param="):
                kv = token[len("param="):]
                if "=" in kv:
                    key, val = kv.split("=", 1)
                    if val == "<ask-agent>" and _IDENT_PARAM_RE.match(key):
                        out.append(f"param={key}={identifier}")
                        continue
            out.append(token)
        return " ".join(out)



class EntityCollectionService:
    """CRUD + membership + add-in/remove-out audit log + rule-based sync for
    `entity_collections` (named groups of entities — watchlists/portfolios).

    Mirrors RegistryService: thin orchestration over SQLAlchemy models, one
    shared session, idempotent writes. `add_entity_to_collection` /
    `remove_entity_from_collection` append to `entity_collection_changes` on
    every real transition (and are no-ops when the membership is already in
    the target state). `sync_entity_collection` re-derives rule-based
    membership and records every add_in/remove_out with source='cron'.
    """

    def __init__(self, session: Session):
        self._session = session

    # ── collection CRUD ──────────────────────────────────────────

    def create_entity_collection(
        self,
        name: str,
        description: Optional[str] = None,
        rule: Optional[dict] = None,
        rule_script: Optional[str] = None,
        rule_id: Optional[int] = None,
    ) -> dict:
        provided = [x for x in (rule, rule_script, rule_id) if x is not None]
        if len(provided) > 1:
            raise ValueError(
                "a collection may have at most one of rule_id, rule, rule_script"
            )
        existing = (
            self._session.query(EntityCollection)
            .filter(EntityCollection.name == name)
            .first()
        )
        if existing is not None:
            raise ValueError(f"Entity collection '{name}' already exists")
        coll = EntityCollection(
            name=name,
            description=description,
            rule_json=rule,
            rule_script=rule_script,
            rule_id=rule_id,
        )
        self._session.add(coll)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return coll.to_dict()

    def list_entity_collections(self) -> list[dict]:
        rows = (
            self._session.query(EntityCollection)
            .order_by(EntityCollection.name)
            .all()
        )
        return [r.to_dict() for r in rows]

    def get_entity_collection(self, name: str) -> dict:
        coll = self._get_collection(name)
        d = coll.to_dict()
        d["members"] = [self._member_detail(i) for i in self._ordered_items(coll.id)]
        return d

    def update_entity_collection(
        self,
        name: str,
        new_name: Optional[str] = None,
        description: Optional[str] = None,
        rule: Optional[dict] = None,
        clear_rule: bool = False,
        rule_script: Optional[str] = None,
        rule_id: Optional[int] = None,
    ) -> dict:
        provided = [x for x in (rule, rule_script, rule_id) if x is not None]
        if len(provided) > 1:
            raise ValueError(
                "a collection may have at most one of rule_id, rule, rule_script"
            )
        if (
            new_name is None
            and description is None
            and rule is None
            and rule_script is None
            and rule_id is None
            and not clear_rule
        ):
            raise ValueError(
                "at least one of new_name, description, rule, rule_script, rule_id is required"
            )
        coll = self._get_collection(name)
        if new_name is not None and new_name != name:
            clash = (
                self._session.query(EntityCollection)
                .filter(EntityCollection.name == new_name)
                .first()
            )
            if clash is not None:
                raise ValueError(f"Entity collection '{new_name}' already exists")
            coll.name = new_name
        if description is not None:
            coll.description = description
        if clear_rule:
            coll.rule_json = None
            coll.rule_script = None
            coll.rule_id = None
        else:
            # Mutually exclusive: setting one clears the others so the invariant
            # (at most one rule per collection) holds across updates.
            if rule is not None:
                coll.rule_json = rule
                coll.rule_script = None
                coll.rule_id = None
            if rule_script is not None:
                coll.rule_script = rule_script
                coll.rule_json = None
                coll.rule_id = None
            if rule_id is not None:
                coll.rule_id = rule_id
                coll.rule_json = None
                coll.rule_script = None
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return coll.to_dict()

    def delete_entity_collection(self, name: str) -> dict:
        coll = self._get_collection(name)
        deleted = coll.id
        self._session.delete(coll)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        # items + changes removed by ON DELETE CASCADE
        return {"deleted": deleted, "name": name}

    # ── membership ───────────────────────────────────────────────

    def add_entity_to_collection(
        self,
        collection_name: str,
        entity_id: Optional[int] = None,
        entity_type: Optional[str] = None,
        code: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        coll = self._get_collection(collection_name)
        e = self._resolve_entity(entity_id, entity_type, code)
        existing = (
            self._session.query(EntityCollectionItem)
            .filter(
                EntityCollectionItem.collection_id == coll.id,
                EntityCollectionItem.entity_type == e.entity_type,
                EntityCollectionItem.code == e.code,
            )
            .first()
        )
        if existing is not None:
            return {
                "action": "already_member",
                "collection": coll.name,
                "entity_type": e.entity_type,
                "code": e.code,
            }
        next_order = (
            self._session.query(
                func.coalesce(func.max(EntityCollectionItem.sort_order), -1)
            )
            .filter(EntityCollectionItem.collection_id == coll.id)
            .scalar()
        ) + 1
        item = EntityCollectionItem(
            collection_id=coll.id,
            entity_type=e.entity_type,
            code=e.code,
            sort_order=next_order,
            added_reason=reason,
        )
        self._session.add(item)
        self._session.add(
            EntityCollectionChange(
                collection_id=coll.id,
                entity_type=e.entity_type,
                code=e.code,
                action="add_in",
                source="manual",
                reason=reason,
            )
        )
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {
            "action": "added",
            "collection": coll.name,
            "entity_type": e.entity_type,
            "code": e.code,
            "item": self._member_detail(item),
        }

    def remove_entity_from_collection(
        self,
        collection_name: str,
        entity_id: Optional[int] = None,
        entity_type: Optional[str] = None,
        code: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        coll = self._get_collection(collection_name)
        e = self._resolve_entity(entity_id, entity_type, code)
        item = (
            self._session.query(EntityCollectionItem)
            .filter(
                EntityCollectionItem.collection_id == coll.id,
                EntityCollectionItem.entity_type == e.entity_type,
                EntityCollectionItem.code == e.code,
            )
            .first()
        )
        if item is None:
            return {
                "action": "not_member",
                "collection": coll.name,
                "entity_type": e.entity_type,
                "code": e.code,
            }
        self._session.delete(item)
        self._session.add(
            EntityCollectionChange(
                collection_id=coll.id,
                entity_type=e.entity_type,
                code=e.code,
                action="remove_out",
                source="manual",
                reason=reason,
            )
        )
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {
            "action": "removed",
            "collection": coll.name,
            "entity_type": e.entity_type,
            "code": e.code,
        }

    def list_entity_collection_items(self, collection_name: str) -> dict:
        coll = self._get_collection(collection_name)
        items = self._ordered_items(coll.id)
        return {
            "collection": coll.name,
            "count": len(items),
            "members": [self._member_detail(i) for i in items],
        }

    def reorder_entity_collection_items(
        self, collection_name: str, ordered_item_ids: list[int]
    ) -> dict:
        coll = self._get_collection(collection_name)
        current = {
            i.id: i
            for i in self._session.query(EntityCollectionItem)
            .filter(EntityCollectionItem.collection_id == coll.id)
            .all()
        }
        if set(ordered_item_ids) != set(current.keys()):
            raise ValueError(
                "ordered_item_ids must contain exactly the current item ids of this collection"
            )
        if len(ordered_item_ids) != len(set(ordered_item_ids)):
            raise ValueError("ordered_item_ids contains duplicates")
        for sort_order, item_id in enumerate(ordered_item_ids):
            current[item_id].sort_order = sort_order
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {"collection": coll.name, "ordered": ordered_item_ids}

    # ── audit log ────────────────────────────────────────────────

    def list_entity_collection_changes(
        self,
        collection_name: Optional[str] = None,
        entity_type: Optional[str] = None,
        code: Optional[str] = None,
        action: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        q = self._session.query(EntityCollectionChange)
        coll_id: Optional[int] = None
        if collection_name is not None:
            coll = self._get_collection(collection_name)
            coll_id = coll.id
            q = q.filter(EntityCollectionChange.collection_id == coll_id)
        if entity_type is not None:
            q = q.filter(EntityCollectionChange.entity_type == entity_type)
        if code is not None:
            q = q.filter(EntityCollectionChange.code == code)
        if action is not None:
            if action not in ("add_in", "remove_out"):
                raise ValueError("action must be 'add_in' or 'remove_out'")
            q = q.filter(EntityCollectionChange.action == action)
        if source is not None:
            if source not in ("manual", "cron"):
                raise ValueError("source must be 'manual' or 'cron'")
            q = q.filter(EntityCollectionChange.source == source)
        total = q.count()
        rows = (
            q.order_by(EntityCollectionChange.changed_at.desc(), EntityCollectionChange.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        # Enrich with collection name. Per D5/3.3 the change rows carry the
        # denormalized natural key (entity_type, code), so entity_code comes
        # straight off the row and survives the 3.7 drop of `entities`.
        # entity_name is no longer resolvable locally (the gateway is the
        # entity master); the code is the canonical id.
        coll_names = {c.id: c.name for c in self._session.query(EntityCollection).all()}
        out = []
        for r in rows:
            out.append(
                {
                    **r.to_dict(),
                    "collection_name": coll_names.get(r.collection_id),
                    "entity_code": r.code,
                    "entity_name": None,
                }
            )
        return {"changes": out, "count": len(out), "total": total, "offset": offset}

    # ── sync ─────────────────────────────────────────────────────

    def sync_entity_collection(self, name: str) -> dict:
        coll = self._get_collection(name)
        current_rows = (
            self._session.query(EntityCollectionItem)
            .filter(EntityCollectionItem.collection_id == coll.id)
            .all()
        )
        current_keys = {(r.entity_type, r.code) for r in current_rows}
        rule_obj = self._resolve_rule_for_collection(coll)
        if rule_obj is None:
            return {
                "action": "manual_collection",
                "name": coll.name,
                "added": [],
                "removed": [],
                "unchanged": len(current_keys),
            }
        db_url = str(self._session.bind.url)
        intended_keys = set(RuleEngine.evaluate(rule_obj, self._session, db_url))
        rule_kind = rule_obj.rule_type
        to_add = intended_keys - current_keys
        to_remove = current_keys - intended_keys
        unchanged = len(intended_keys & current_keys)
        # Append at end: sort_order = max(existing) + 1, +1 each.
        next_order = (
            self._session.query(
                func.coalesce(func.max(EntityCollectionItem.sort_order), -1)
            )
            .filter(EntityCollectionItem.collection_id == coll.id)
            .scalar()
        )
        added_details = []
        for etype, ecode in to_add:
            next_order += 1
            item = EntityCollectionItem(
                collection_id=coll.id,
                entity_type=etype,
                code=ecode,
                sort_order=next_order,
                added_reason="sync: rule matched",
            )
            self._session.add(item)
            self._session.add(
                EntityCollectionChange(
                    collection_id=coll.id,
                    entity_type=etype,
                    code=ecode,
                    action="add_in",
                    source="cron",
                    reason="sync: rule matched",
                )
            )
            added_details.append({"entity_type": etype, "code": ecode})
        removed_details = []
        for etype, ecode in to_remove:
            item = (
                self._session.query(EntityCollectionItem)
                .filter(
                    EntityCollectionItem.collection_id == coll.id,
                    EntityCollectionItem.entity_type == etype,
                    EntityCollectionItem.code == ecode,
                )
                .first()
            )
            if item is not None:
                self._session.delete(item)
            self._session.add(
                EntityCollectionChange(
                    collection_id=coll.id,
                    entity_type=etype,
                    code=ecode,
                    action="remove_out",
                    source="cron",
                    reason="sync: rule no longer matches",
                )
            )
            removed_details.append({"entity_type": etype, "code": ecode})
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {
            "action": "synced",
            "name": coll.name,
            "rule": rule_kind,
            "added": added_details,
            "removed": removed_details,
            "unchanged": unchanged,
        }

    # ── helpers ──────────────────────────────────────────────────

    def _get_collection(self, name: str) -> EntityCollection:
        coll = (
            self._session.query(EntityCollection)
            .filter(EntityCollection.name == name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Entity collection '{name}' not found")
        return coll

    def _resolve_entity(
        self,
        entity_id: Optional[int],
        entity_type: Optional[str],
        code: Optional[str],
    ) -> object:
        """Resolve an entity to a namespace carrying id + natural key.

        Per design D5, the fd-open-data-mcp gateway is the entity master and
        the local ``entities`` table is dropped in 3.7, so the natural key
        ``(entity_type, code)`` is the only resolvable form. ``entity_id``-
        only resolution is no longer possible (no local entity master; the
        gateway has no id→natural-key tool).
        """
        if entity_id is not None:
            raise ValueError(
                "entity_id resolution is no longer available (entities table "
                "dropped in D5/3.7); provide entity_type + code"
            )
        if entity_type is None or code is None:
            raise ValueError("provide both entity_type and code")

        # Gateway-first (fd-open-data-mcp is the entity master per D5).
        resolved = self._resolve_entity_via_gateway(entity_type, code)
        if resolved is not None:
            return resolved

        # Gateway unavailable (or entity not found there): proceed with the
        # caller-supplied natural key. The membership write path only needs
        # entity_type + code — a watchlist can hold any (type, code) tuple,
        # mirroring how rule scripts return arbitrary strings.
        from types import SimpleNamespace

        return SimpleNamespace(
            id=None, entity_type=entity_type, code=code, name=None
        )

    def _resolve_entity_via_gateway(
        self,
        entity_type: str,
        code: str,
    ) -> Optional[object]:
        """Resolve ``(entity_type, code)`` via the fd-open-data-mcp gateway.

        Returns a lightweight namespace with ``.id``/``.entity_type``/``.code``
        (the only attributes the membership write path uses), or ``None`` if
        the gateway is unavailable or the entity is not found there (caller
        falls back to the caller-supplied natural key).
        """
        sync_call = self._load_gateway_sync()
        if sync_call is None:
            return None  # gateway bridge unavailable → local fallback
        try:
            resp = sync_call(
                "fd-open-data-mcp",
                "get_entity",
                json.dumps({"entity_type": entity_type, "code": code}),
            )
        except Exception:
            # gateway call failed (no event loop, transport error, …) →
            # fall back to the local table.
            return None
        if not isinstance(resp, dict) or "error" in resp:
            return None  # gateway error → local fallback
        data = resp.get("result")
        if not isinstance(data, dict) or not data.get("id"):
            return None  # entity not found in gateway → local fallback
        from types import SimpleNamespace

        return SimpleNamespace(
            id=data["id"],
            entity_type=data.get("entity_type") or entity_type,
            code=data.get("code") or code,
            name=data.get("name_en") or data.get("name_zh"),
        )

    @staticmethod
    def _load_gateway_sync():
        """Lazily load the sync gateway bridge (``call_data_mcp_sync``).

        The bridge lives in ``gateway-mcp/gateway_tools.py``; at tool-call time
        that directory is not on ``sys.path`` (the registry evicts per-group
        source modules after harvest), so we insert it temporarily. Returns
        the callable, or ``None`` if it cannot be imported (gateway
        unavailable → callers fall back to local resolution).
        """
        try:
            import sys
            from pathlib import Path

            gateway_dir = str(Path(__file__).resolve().parents[1] / "gateway-mcp")
            if gateway_dir not in sys.path:
                sys.path.insert(0, gateway_dir)
            from gateway_tools import call_data_mcp_sync  # type: ignore

            return call_data_mcp_sync
        except Exception:
            return None

    def _ordered_items(self, collection_id: int) -> list:
        return (
            self._session.query(EntityCollectionItem)
            .filter(EntityCollectionItem.collection_id == collection_id)
            .order_by(EntityCollectionItem.sort_order, EntityCollectionItem.id)
            .all()
        )

    def _member_detail(self, item: EntityCollectionItem) -> dict:
        d = item.to_dict()
        # D5: the item carries the denormalized natural key (entity_type, code);
        # there is no local entity master post-3.7 (entities dropped), so the
        # display fields (name/ticker/exchange/country_code) are no longer
        # resolvable here — the code is the canonical id.
        d["entity_type"] = item.entity_type
        d["code"] = item.code
        d["name"] = None
        d["ticker"] = None
        d["exchange"] = None
        d["country_code"] = None
        return d

    def _resolve_rule_for_collection(self, coll: EntityCollection):
        """Resolve a collection's rule to an engine-evaluable object, with the
        D8 precedence: `rule_id` (any rule_type) -> legacy `rule_script`
        (treated as a `script` rule) -> legacy `rule_json` (treated as a `json`
        rule) -> None (manual collection). Returns a `Rule` model instance, a
        legacy shim, or None.
        """
        if coll.rule_id is not None:
            rule = self._session.get(Rule, coll.rule_id)
            if rule is not None:
                return rule
            # stale rule_id (the rule was deleted) -> fall through to legacy/manual
        if coll.rule_script is not None:
            return legacy_shim("script", script_config_from_path(coll.rule_script), "entity_ids")
        if coll.rule_json is not None:
            return legacy_shim("json", coll.rule_json, "entity_ids")
        return None

    def _rule_entity_ids(self, rule: dict) -> list[tuple[str, str]]:
        """Apply a legacy declarative `rule_json` filter and return matching
        natural keys ``(entity_type, code)``. Delegates to the RuleEngine (json
        type). Kept for `entity_collection_sync.py --dry-run` and the selfchecks.
        """
        return RuleEngine.evaluate(legacy_shim("json", rule, "entity_ids"), self._session)

    # ── script rule ──────────────────────────────────────────────

    def _script_entity_ids(self, script_path: str) -> list[tuple[str, str]]:
        """Load a rule script, call `members(ctx)`, normalize the result to
        natural keys ``(entity_type, code)``. The script path is repo-root
        relative (or absolute); it runs with a read-only `RuleContext` so it
        can SELECT from any daas.db table. Items that don't resolve to a
        known entity are skipped — a sync shouldn't fail the whole collection
        over one delisted code.
        """
        return RuleEngine.evaluate(
            legacy_shim("script", script_config_from_path(script_path), "entity_ids"),
            self._session,
            str(self._session.bind.url),
        )


class IndicatorCollectionService:
    """CRUD + membership + add-in/remove-out audit log for `indicator_collections`
    (named groups of indicators), with a 3-level per-item score resolution:
    `COALESCE(item.score, indicator_rules.score, sources.score)` — item override
    → indicator default → datasource default.

    Mirrors EntityCollectionService: thin orchestration over SQLAlchemy models,
    one shared session, idempotent writes. `add_indicator_to_collection` /
    `remove_indicator_from_collection` append to `indicator_collection_changes`
    on every real transition (and are no-ops when the membership is already in
    the target state). Indicator membership is a real FK→indicator_rules.id
    with ON DELETE CASCADE; the audit row is denormalized on `indicator_name`
    so it survives indicator-rule deletion.
    """

    def __init__(self, session: Session):
        self._session = session

    # ── collection CRUD ──────────────────────────────────────────

    def create_indicator_collection(
        self,
        name: str,
        description: Optional[str] = None,
        rule_id: Optional[int] = None,
    ) -> dict:
        existing = (
            self._session.query(IndicatorCollection)
            .filter(IndicatorCollection.name == name)
            .first()
        )
        if existing is not None:
            raise ValueError(f"Indicator collection '{name}' already exists")
        coll = IndicatorCollection(name=name, description=description, rule_id=rule_id)
        self._session.add(coll)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return coll.to_dict()

    def list_indicator_collections(self) -> list[dict]:
        rows = (
            self._session.query(IndicatorCollection)
            .order_by(IndicatorCollection.name)
            .all()
        )
        return [r.to_dict() for r in rows]

    def get_indicator_collection(self, name: str) -> dict:
        coll = self._get_collection(name)
        d = coll.to_dict()
        d["items"] = [self._item_detail(i) for i in self._ordered_items(coll.id)]
        return d

    def update_indicator_collection(
        self,
        name: str,
        new_name: Optional[str] = None,
        description: Optional[str] = None,
        rule_id: Optional[int] = None,
        clear_rule: bool = False,
    ) -> dict:
        if new_name is None and description is None and rule_id is None and not clear_rule:
            raise ValueError("at least one of new_name, description, rule_id is required")
        coll = self._get_collection(name)
        if new_name is not None and new_name != name:
            clash = (
                self._session.query(IndicatorCollection)
                .filter(IndicatorCollection.name == new_name)
                .first()
            )
            if clash is not None:
                raise ValueError(f"Indicator collection '{new_name}' already exists")
            coll.name = new_name
        if description is not None:
            coll.description = description
        if clear_rule:
            coll.rule_id = None
        elif rule_id is not None:
            coll.rule_id = rule_id
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return coll.to_dict()

    def delete_indicator_collection(self, name: str) -> dict:
        coll = self._get_collection(name)
        deleted = coll.id
        self._session.delete(coll)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        # items + changes removed by ON DELETE CASCADE
        return {"deleted": deleted, "name": name}

    # ── membership ───────────────────────────────────────────────

    def sync_indicator_collection(self, name: str) -> dict:
        """Re-derive the member set for a rule-based indicator collection by
        evaluating its rule (target='indicator_names') via the RuleEngine, diff
        vs current members, apply add_in/remove_out (source='cron'), and record
        every transition. A manual collection (rule_id NULL) is a no-op.
        """
        coll = self._get_collection(name)
        rule_obj = self._resolve_rule_for_indicator_collection(coll)
        current_rows = (
            self._session.query(IndicatorCollectionItem)
            .filter(IndicatorCollectionItem.collection_id == coll.id)
            .all()
        )
        # indicator_id -> indicator_name (denormalized for the audit log).
        id_to_name = {
            i.indicator_id: (i.indicator.name if i.indicator else None)
            for i in current_rows
        }
        current_ids = {i.indicator_id for i in current_rows}
        if rule_obj is None:
            return {
                "action": "manual_collection",
                "name": coll.name,
                "added": [],
                "removed": [],
                "unchanged": len(current_ids),
            }
        if getattr(rule_obj, "target", "indicator_names") != "indicator_names":
            return {
                "success": False,
                "error": (
                    f"indicator collection rule must have target='indicator_names' "
                    f"(got {rule_obj.target!r})"
                ),
            }
        db_url = str(self._session.bind.url)
        intended_names = {str(n) for n in RuleEngine.evaluate(rule_obj, self._session, db_url)}
        # Resolve intended names -> indicator ids (unknown names skipped).
        name_to_id: dict[str, int] = {}
        for name in intended_names:
            ind = (
                self._session.query(IndicatorRule)
                .filter(IndicatorRule.name == name)
                .first()
            )
            if ind is not None:
                name_to_id[name] = ind.id
        intended_ids = set(name_to_id.values())
        to_add = intended_ids - current_ids
        to_remove = current_ids - intended_ids
        unchanged = len(intended_ids & current_ids)
        next_order = (
            self._session.query(
                func.coalesce(func.max(IndicatorCollectionItem.sort_order), -1)
            )
            .filter(IndicatorCollectionItem.collection_id == coll.id)
            .scalar()
        )
        added_names = []
        for iid in to_add:
            next_order += 1
            ind = self._session.get(IndicatorRule, iid)
            iname = ind.name if ind else name_to_id.get(iid)
            self._session.add(
                IndicatorCollectionItem(
                    collection_id=coll.id,
                    indicator_id=iid,
                    sort_order=next_order,
                )
            )
            self._session.add(
                IndicatorCollectionChange(
                    collection_id=coll.id,
                    indicator_name=iname,
                    action="add_in",
                    source="cron",
                    reason="sync: rule matched",
                )
            )
            added_names.append(iname)
        removed_names = []
        for iid in to_remove:
            iname = id_to_name.get(iid)
            item = (
                self._session.query(IndicatorCollectionItem)
                .filter(
                    IndicatorCollectionItem.collection_id == coll.id,
                    IndicatorCollectionItem.indicator_id == iid,
                )
                .first()
            )
            if item is not None:
                self._session.delete(item)
            self._session.add(
                IndicatorCollectionChange(
                    collection_id=coll.id,
                    indicator_name=iname,
                    action="remove_out",
                    source="cron",
                    reason="sync: rule no longer matches",
                )
            )
            removed_names.append(iname)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {
            "action": "synced",
            "name": coll.name,
            "rule": rule_obj.rule_type,
            "added": added_names,
            "removed": removed_names,
            "unchanged": unchanged,
        }

    def _resolve_rule_for_indicator_collection(self, coll: IndicatorCollection):
        """Resolve an indicator collection's rule. Indicator collections have
        no legacy rule_json/rule_script, so this is `rule_id` -> None (manual)."""
        if coll.rule_id is not None:
            rule = self._session.get(Rule, coll.rule_id)
            if rule is not None:
                return rule
        return None

    def add_indicator_to_collection(
        self,
        collection_name: str,
        indicator_name: str,
        score: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> dict:
        coll = self._get_collection(collection_name)
        ind = self._resolve_indicator(indicator_name)
        existing = (
            self._session.query(IndicatorCollectionItem)
            .filter(
                IndicatorCollectionItem.collection_id == coll.id,
                IndicatorCollectionItem.indicator_id == ind.id,
            )
            .first()
        )
        if existing is not None:
            return {
                "action": "already_member",
                "collection": coll.name,
                "indicator_name": ind.name,
            }
        next_order = (
            self._session.query(
                func.coalesce(func.max(IndicatorCollectionItem.sort_order), -1)
            )
            .filter(IndicatorCollectionItem.collection_id == coll.id)
            .scalar()
        ) + 1
        item = IndicatorCollectionItem(
            collection_id=coll.id,
            indicator_id=ind.id,
            sort_order=next_order,
            score=score,
        )
        self._session.add(item)
        self._session.add(
            IndicatorCollectionChange(
                collection_id=coll.id,
                indicator_name=ind.name,
                action="add_in",
                source="manual",
                reason=reason,
            )
        )
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(item)
        return {
            "action": "added",
            "collection": coll.name,
            "indicator_name": ind.name,
            "item": self._item_detail(item),
        }

    def remove_indicator_from_collection(
        self,
        collection_name: str,
        indicator_name: str,
        reason: Optional[str] = None,
    ) -> dict:
        coll = self._get_collection(collection_name)
        ind = self._resolve_indicator(indicator_name)
        item = (
            self._session.query(IndicatorCollectionItem)
            .filter(
                IndicatorCollectionItem.collection_id == coll.id,
                IndicatorCollectionItem.indicator_id == ind.id,
            )
            .first()
        )
        if item is None:
            return {
                "action": "not_member",
                "collection": coll.name,
                "indicator_name": ind.name,
            }
        self._session.delete(item)
        self._session.add(
            IndicatorCollectionChange(
                collection_id=coll.id,
                indicator_name=ind.name,
                action="remove_out",
                source="manual",
                reason=reason,
            )
        )
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {
            "action": "removed",
            "collection": coll.name,
            "indicator_name": ind.name,
        }

    def list_indicator_collection_items(self, collection_name: str) -> dict:
        coll = self._get_collection(collection_name)
        items = self._ordered_items(coll.id)
        return {
            "collection": coll.name,
            "count": len(items),
            "items": [self._item_detail(i) for i in items],
        }

    def reorder_indicator_collection_items(
        self, collection_name: str, ordered_item_ids: list[int]
    ) -> dict:
        coll = self._get_collection(collection_name)
        current = {
            i.id: i
            for i in self._session.query(IndicatorCollectionItem)
            .filter(IndicatorCollectionItem.collection_id == coll.id)
            .all()
        }
        if set(ordered_item_ids) != set(current.keys()):
            raise ValueError(
                "ordered_item_ids must contain exactly the current item ids of this collection"
            )
        if len(ordered_item_ids) != len(set(ordered_item_ids)):
            raise ValueError("ordered_item_ids contains duplicates")
        for sort_order, item_id in enumerate(ordered_item_ids):
            current[item_id].sort_order = sort_order
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return {"collection": coll.name, "ordered": ordered_item_ids}

    def set_indicator_collection_item_score(
        self,
        collection_name: str,
        indicator_name: str,
        score: Optional[float] = None,
    ) -> dict:
        """Set the per-item `score` override (float) or clear it (None →
        inherit the indicator's default `indicator_rules.score`, which itself
        inherits the datasource default when NULL)."""
        coll = self._get_collection(collection_name)
        ind = self._resolve_indicator(indicator_name)
        item = (
            self._session.query(IndicatorCollectionItem)
            .filter(
                IndicatorCollectionItem.collection_id == coll.id,
                IndicatorCollectionItem.indicator_id == ind.id,
            )
            .first()
        )
        if item is None:
            raise ValueError(
                f"indicator '{indicator_name}' is not in collection '{collection_name}'"
            )
        if score is not None and not isinstance(score, (int, float)):
            raise ValueError("score must be a number or null")
        item.score = float(score) if score is not None else None
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(item)
        return self._item_detail(item)

    # ── audit log ────────────────────────────────────────────────

    def list_indicator_collection_changes(
        self,
        collection_name: Optional[str] = None,
        action: Optional[str] = None,
        source: Optional[str] = None,
        indicator_name: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        q = self._session.query(IndicatorCollectionChange)
        if collection_name is not None:
            coll = self._get_collection(collection_name)
            q = q.filter(IndicatorCollectionChange.collection_id == coll.id)
        if action is not None:
            if action not in ("add_in", "remove_out"):
                raise ValueError("action must be 'add_in' or 'remove_out'")
            q = q.filter(IndicatorCollectionChange.action == action)
        if source is not None:
            if source not in ("manual", "cron"):
                raise ValueError("source must be 'manual' or 'cron'")
            q = q.filter(IndicatorCollectionChange.source == source)
        if indicator_name is not None:
            q = q.filter(IndicatorCollectionChange.indicator_name == indicator_name)
        total = q.count()
        rows = (
            q.order_by(
                IndicatorCollectionChange.changed_at.desc(),
                IndicatorCollectionChange.id.desc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        coll_names = {c.id: c.name for c in self._session.query(IndicatorCollection).all()}
        out = [
            {**r.to_dict(), "collection_name": coll_names.get(r.collection_id)}
            for r in rows
        ]
        return {"changes": out, "count": len(out), "total": total, "offset": offset}

    # ── helpers ──────────────────────────────────────────────────

    def _get_collection(self, name: str) -> IndicatorCollection:
        coll = (
            self._session.query(IndicatorCollection)
            .filter(IndicatorCollection.name == name)
            .first()
        )
        if coll is None:
            raise ValueError(f"Indicator collection '{name}' not found")
        return coll

    def _resolve_indicator(self, name: str) -> IndicatorRule:
        ind = (
            self._session.query(IndicatorRule)
            .filter(IndicatorRule.name == name)
            .first()
        )
        if ind is None:
            raise ValueError(f"Indicator '{name}' not found")
        return ind

    def _ordered_items(self, collection_id: int) -> list:
        return (
            self._session.query(IndicatorCollectionItem)
            .filter(IndicatorCollectionItem.collection_id == collection_id)
            .order_by(IndicatorCollectionItem.sort_order, IndicatorCollectionItem.id)
            .all()
        )

    def _datasource_scores(self) -> dict:
        """Map every daas sources.name → its default `score`."""
        return {
            n: s for (n, s) in self._session.query(DaasSource.name, DaasSource.score).all()
        }

    def _item_detail(self, item: IndicatorCollectionItem) -> dict:
        """Item dict + the 3-level effective score resolution:
        item.score → indicator_rules.score → sources.score → NULL.

        Returns `item_score` (raw override, NULL = inherit), `indicator_default_score`,
        `source_default_score`, and `score` (resolved effective).
        """
        d = item.to_dict()
        ind = item.indicator
        ds_scores = self._datasource_scores()
        ind_score = ind.score if ind is not None else None
        ds_score = ds_scores.get(ind.datasource) if ind is not None else None
        d["indicator_name"] = ind.name if ind is not None else None
        # Raw per-item override (NULL = inherit). `to_dict()` already set
        # `score` to item.score; rename it to `item_score` and recompute the
        # resolved `score` below.
        d["item_score"] = item.score
        d["indicator_default_score"] = ind_score
        d["source_default_score"] = ds_score
        # 3-level resolution: item override → indicator default → datasource default.
        if item.score is not None:
            d["score"] = item.score
        elif ind_score is not None:
            d["score"] = ind_score
        else:
            d["score"] = ds_score
        return d
