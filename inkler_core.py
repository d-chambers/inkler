"""Structured SVG edits. Uses Ubuntu's lxml/inkex, never evaluates Python."""
import copy
import hashlib
import re
from lxml import etree

SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"
SOD = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
XLINK = "http://www.w3.org/1999/xlink"
NS = {"svg": SVG, "inkscape": INK, "sodipodi": SOD, "xlink": XLINK}
TAGS = frozenset("g defs path rect circle ellipse line polyline polygon text tspan textPath title desc linearGradient radialGradient stop marker clipPath mask pattern use".split())
ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)


def revision(root):
    clone = copy.deepcopy(root)
    # Zoom, current layer and viewport are human UI state, not drawing content.
    for node in clone.xpath("//sodipodi:namedview", namespaces=NS):
        node.getparent().remove(node)
    return hashlib.sha256(etree.tostring(clone, method="c14n")).hexdigest()


def safe_attribute(name, value):
    q = etree.QName(name)
    local = q.localname.lower()
    if q.namespace not in (None, INK, XLINK) or local.startswith("on"):
        raise ValueError("Unsupported or active attribute: " + name)
    if local in {"base", "src"}:
        raise ValueError("External resources are not supported")
    if not isinstance(value, str) or len(value) > 200000:
        raise ValueError("Attribute values must be strings under 200k characters")
    if local == "href" and not re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_.-]*", value):
        raise ValueError("Only internal #id references are supported")
    lowered = value.lower()
    if any(s in lowered for s in ("@import", "javascript:", "expression(", "\\", "/*", "*/")):
        raise ValueError("Active/obfuscated CSS is not supported")
    for match in URL.finditer(value):
        if not re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_.-]*", match[2]):
            raise ValueError("Only internal url(#id) references are supported")
    if "url" in lowered and "(" in lowered and not URL.search(value):
        raise ValueError("Malformed URL reference")
    if local == "id" and not ID.fullmatch(value):
        raise ValueError("Use a simple SVG id, e.g. cat-body")


def fragment(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 1000000:
        raise ValueError("SVG fragment must contain 1–1,000,000 characters")
    if "<!" in text or "<?" in text:
        raise ValueError("Declarations, comments and processing instructions are not supported")
    declarations = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
    wrapper = f'<g xmlns="{SVG}" {declarations}>{text}</g>'
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(wrapper.encode(), parser)
    nodes = list(root)
    if not nodes or (root.text or "").strip():
        raise ValueError("Supply SVG elements, with text inside <text> or <tspan>")
    if len(list(root.iter())) > 5000:
        raise ValueError("SVG fragment exceeds 5,000 elements")
    for node in root.iterdescendants():
        q = etree.QName(node)
        if q.namespace != SVG or q.localname not in TAGS:
            raise ValueError("Unsupported SVG element: " + q.localname)
        for key, val in node.attrib.items():
            safe_attribute(key, val)
    return nodes


def find(root, object_id):
    if not isinstance(object_id, str) or not object_id:
        raise ValueError("An object id is required")
    nodes = etree.XPath("//*[@id=$id]")(root, id=object_id)
    if len(nodes) != 1:
        raise ValueError(f"Expected exactly one object with id {object_id!r}, found {len(nodes)}")
    return nodes[0]


def verify_ids(root):
    ids = [el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")]
    if len(ids) != len(set(ids)):
        raise ValueError("Edit would create duplicate SVG ids")


def apply_edits(root, operations, expected_revision):
    if not isinstance(expected_revision, str) or expected_revision != revision(root):
        raise ValueError("Drawing changed since inspection. Inspect again before editing.")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError("Supply 1–100 edit operations")
    result = copy.deepcopy(root)
    for op in operations:
        if not isinstance(op, dict):
            raise ValueError("Each operation must be an object")
        action = op.get("op")
        if action == "add":
            parent = find(result, op["parent"]) if op.get("parent") else result
            if etree.QName(parent).localname not in {"svg", "g", "defs", "marker", "clipPath", "mask", "pattern"}:
                raise ValueError("Parent must be an SVG container")
            for node in fragment(op.get("svg")):
                parent.append(node)
        elif action in {"replace", "remove", "set", "text"}:
            target = find(result, op.get("id"))
            if target is result:
                raise ValueError("Cannot edit the document root")
            if action == "replace":
                nodes = fragment(op.get("svg"))
                parent = target.getparent()
                index = parent.index(target)
                parent.remove(target)
                for offset, node in enumerate(nodes):
                    parent.insert(index + offset, node)
            elif action == "remove":
                target.getparent().remove(target)
            elif action == "set":
                attrs = op.get("attrs")
                if not isinstance(attrs, dict) or not attrs:
                    raise ValueError("set requires a nonempty attrs object")
                for key, val in attrs.items():
                    # Prefixes in JSON attributes use familiar SVG namespace names.
                    name = key
                    if ":" in key and not key.startswith("{"):
                        prefix, local = key.split(":", 1)
                        if prefix not in NS:
                            raise ValueError("Unknown namespace prefix")
                        name = f"{{{NS[prefix]}}}{local}"
                    if val is None:
                        safe_attribute(name, "")
                        target.attrib.pop(name, None)
                    else:
                        safe_attribute(name, val)
                        target.set(name, val)
            else:
                if etree.QName(target).localname not in {"text", "tspan", "title", "desc"}:
                    raise ValueError("text operation requires a text element")
                text = op.get("text")
                if not isinstance(text, str) or len(text) > 100000:
                    raise ValueError("text must be a string under 100k characters")
                for child in list(target):
                    target.remove(child)
                target.text = text
        else:
            raise ValueError("Unknown edit operation: " + str(action))
    verify_ids(result)
    return result


def inspect(root, selected=(), ids=None):
    current_revision = revision(root)
    # inkex's geometry helpers normalize transform attributes when accessed.
    # Work on a copy so inspecting cannot modify the drawing or its revision.
    root = copy.deepcopy(root)
    wanted = None if ids is None else set(ids)
    rows = []
    truncated = False
    for node in root.iter():
        if not isinstance(node.tag, str) or not node.get("id"):
            continue
        object_id = node.get("id")
        if wanted is not None and object_id not in wanted:
            continue
        if len(rows) >= 1500:
            truncated = True
            break
        row = {"id": object_id, "tag": etree.QName(node).localname,
               "parent": node.getparent().get("id") if node.getparent() is not None else None,
               "label": node.get(f"{{{INK}}}label"), "attrs": dict(node.attrib)}
        if row["tag"] in {"text", "tspan", "title", "desc"}:
            row["text"] = "".join(node.itertext())[:2000]
        # inkex geometry omits stroke and approximates text; rendered preview is authoritative.
        try:
            parent = node.getparent()
            transform = parent.composed_transform() if parent is not None else None
            bbox = node.bounding_box(transform)
            if bbox is not None:
                row["bbox"] = [float(bbox.left), float(bbox.top), float(bbox.width), float(bbox.height)]
        except (AttributeError, ValueError, TypeError, ZeroDivisionError):
            pass
        rows.append(row)
    return {"revision": current_revision, "docname": root.get(f"{{{SOD}}}docname", ""),
            "selection": list(selected), "viewBox": root.get("viewBox"),
            "width": root.get("width"), "height": root.get("height"),
            "objects": rows, "truncated": truncated,
            "coordinates": "SVG user units; bbox is [x,y,width,height] in document space, geometry only. Edits use parent coordinates; transforms are NOT copied on replace."}
