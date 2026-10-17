"""One configurable scope for dated announcements and publication searches."""

from dataclasses import dataclass
import re
import unicodedata


def _strings(value, label):
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        raise ValueError(f"{label} must be a list of nonempty strings")
    return tuple(dict.fromkeys(v.strip() for v in value))


def _categories(value):
    values = _strings(value, "arXiv categories")
    if any(
        not re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)?", v)
        for v in values
    ):
        raise ValueError("invalid arXiv category")
    return values


def _normalise(value):
    return re.sub(
        r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()
    ).strip()


def _in_category(category, configured):
    return category == configured or category.startswith(configured + ".")


@dataclass(frozen=True)
class TopicGroup:
    name: str
    categories: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class ArxivSearchScope:
    full_categories: tuple[str, ...]
    topic_groups: tuple[TopicGroup, ...]

    @classmethod
    def from_config(cls, config=None):
        config = {} if config is None else config
        if not isinstance(config, dict):
            raise ValueError("arXiv search_scope must be a mapping")
        full = _categories(config.get("full_categories", ["q-fin"]))
        groups = config.get("topic_groups", [])
        if not isinstance(groups, list):
            raise ValueError("topic_groups must be a list")
        parsed, names = [], set()
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("each topic group must be a mapping")
            name = group.get("name")
            if not isinstance(name, str) or not name.strip() or name.strip() in names:
                raise ValueError("topic groups need unique nonempty names")
            categories = _categories(group.get("categories"))
            keywords = _strings(group.get("keywords"), "topic keywords")
            if (
                not categories
                or not keywords
                or any(not _normalise(k) for k in keywords)
            ):
                raise ValueError("topic groups need categories and searchable keywords")
            names.add(name.strip())
            parsed.append(TopicGroup(name.strip(), categories, keywords))
        if not full and not parsed:
            raise ValueError("arXiv search_scope must select at least one category")
        return cls(full, tuple(parsed))

    @property
    def categories(self):
        return tuple(
            dict.fromkeys(
                (
                    *self.full_categories,
                    *(c for g in self.topic_groups for c in g.categories),
                )
            )
        )

    def match(self, listed_categories, title, abstract):
        texts = [" " + _normalise(field) + " " for field in (title, abstract)]
        full = [
            c
            for c in self.full_categories
            if any(_in_category(v, c) for v in listed_categories)
        ]
        matches = {}
        for group in self.topic_groups:
            if not any(
                _in_category(v, c) for v in listed_categories for c in group.categories
            ):
                continue
            keywords = [
                k
                for k in group.keywords
                if any(" " + _normalise(k) + " " in text for text in texts)
            ]
            if keywords:
                matches[group.name] = keywords
        return (
            {
                "full_categories": full,
                "topic_groups": list(matches),
                "keywords": matches,
            }
            if full or matches
            else {}
        )

    def query(self):
        def cat(value):
            return "cat:" + value + (".*" if "." not in value else "")

        parts = [cat(c) for c in self.full_categories]
        for group in self.topic_groups:
            categories = " OR ".join(cat(c) for c in group.categories)
            terms = []
            for keyword in group.keywords:
                escaped = keyword.replace("\\", "\\\\").replace('"', '\\"')
                terms.extend(f'{field}:"{escaped}"' for field in ("ti", "abs"))
            parts.append(f"(({categories}) AND ({' OR '.join(terms)}))")
        return " OR ".join(parts)
