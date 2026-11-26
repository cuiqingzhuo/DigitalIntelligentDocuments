import heapq
import math
import re
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Point(Strict):
    id: str = Field(pattern=IDENTIFIER.pattern)
    label: str = Field(min_length=1, max_length=100)
    kind: str = Field(pattern=r"^(entrance|junction|service|shelf)$")
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)


class Edge(Strict):
    a: str
    b: str
    accessible: bool = True


class Face(Strict):
    id: str = Field(pattern=IDENTIFIER.pattern)
    label: str = Field(min_length=1, max_length=100)
    shelf: str = Field(min_length=1, max_length=80)
    side: str = Field(pattern=r"^[AB]$")
    direction: str = Field(pattern=r"^(北|南|东|西)$")
    node_id: str
    marker_status: str = Field(default="active", pattern=r"^(active|damaged|retired)$")


class ShelfBlock(Strict):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)
    width: float = Field(gt=0, le=100)
    height: float = Field(gt=0, le=100)
    label: str = Field(min_length=1, max_length=80)


class FloorMap(Strict):
    name: str = Field(min_length=1, max_length=100)
    service_label: str = Field(default="一楼入口服务台", min_length=1, max_length=100)
    image: str | None = Field(default=None, max_length=4_000_000)
    nodes: list[Point] = Field(max_length=1000)
    edges: list[Edge] = Field(max_length=5000)
    faces: list[Face] = Field(max_length=500)
    blocks: list[ShelfBlock] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def check_links(self):
        points = {node.id: node for node in self.nodes}
        if len(points) != len(self.nodes) or len({face.id for face in self.faces}) != len(self.faces):
            raise ValueError("节点和架面编号必须唯一")
        if len({(face.shelf, face.side) for face in self.faces}) != len(self.faces):
            raise ValueError("同一书架的 A/B 面只允许各定义一次")
        seen = set()
        for edge in self.edges:
            key = frozenset((edge.a, edge.b))
            if len(key) != 2 or not key <= points.keys() or key in seen:
                raise ValueError("路线边不得重复，且必须关联两个不同的有效节点")
            if (points[edge.a].x, points[edge.a].y) == (points[edge.b].x, points[edge.b].y):
                raise ValueError("路线边的两端坐标不能重合")
            seen.add(key)
        if any(face.node_id not in points or points[face.node_id].kind != "shelf" for face in self.faces):
            raise ValueError("每个架面必须关联一个架面节点")
        if any(block.x + block.width > 100 or block.y + block.height > 100 for block in self.blocks):
            raise ValueError("书架区域超出底图")
        return self


def route(document, start, face_id):
    nodes = {node["id"]: node for node in document["nodes"]}
    faces = {face["id"]: face for face in document["faces"]}
    origin = faces[start]["node_id"] if start in faces else start
    target = faces.get(face_id)
    if origin not in nodes or not target or target["marker_status"] == "retired":
        return []
    goal = target["node_id"]

    def distance(a, b):
        return math.hypot(nodes[a]["x"] - nodes[b]["x"], nodes[a]["y"] - nodes[b]["y"])

    graph = {node: [] for node in nodes}
    for edge in document["edges"]:
        if edge["accessible"]:
            a, b = edge["a"], edge["b"]
            graph[a].append(b)
            graph[b].append(a)
    queue = [(distance(origin, goal), 0, origin)]
    costs = {origin: 0}
    previous = {}
    while queue:
        _, cost, node = heapq.heappop(queue)
        if cost != costs[node]:
            continue
        if node == goal:
            path = [node]
            while path[-1] in previous:
                path.append(previous[path[-1]])
            return [nodes[item] for item in reversed(path)]
        for neighbor in graph[node]:
            proposed = cost + distance(node, neighbor)
            if proposed < costs.get(neighbor, math.inf):
                costs[neighbor] = proposed
                previous[neighbor] = node
                heapq.heappush(queue, (proposed + distance(neighbor, goal), proposed, neighbor))
    return []


def marker(value, library, floor):
    if len(value) > 500:
        raise ValueError("架面编号过长")
    if IDENTIFIER.fullmatch(value):
        return value.upper()
    uri = urlsplit(value)
    parts = [unquote(part) for part in uri.path.strip("/").split("/")]
    if uri.scheme in ("http", "https") and len(parts) == 4 and parts[0] == "shelf":
        _, actual_library, actual_floor, face = parts
    elif uri.scheme == "diw" and uri.netloc == "shelf" and len(parts) == 3:
        actual_library, actual_floor, face = parts
    else:
        raise ValueError("无法读取架标二维码")
    if actual_library != library or actual_floor != floor or not IDENTIFIER.fullmatch(face):
        raise ValueError("此架标属于其他馆或楼层")
    return face.upper()
