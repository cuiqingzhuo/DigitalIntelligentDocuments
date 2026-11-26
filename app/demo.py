from sqlalchemy import select

from .recognition import BASELINE
from .routing import FloorMap
from .storage import editions, holdings, map_history, maps, models, now

LIBRARY = "demo"
FLOOR = "F1"
BOOKS = [
    ("DS-3", "数据结构", "严蔚敏、吴伟民", "清华大学出版社", 2022, "第3版", "9787302511835", "TP311.12/03"),
    ("DS-2", "数据结构", "严蔚敏、吴伟民", "清华大学出版社", 2011, "第2版", None, "TP311.12/02"),
    ("PY-1", "Python 编程", "埃里克·马瑟斯", "人民邮电出版社", 2023, "第3版", None, "TP311.56/18"),
    ("ALG-1", "算法导论", "托马斯·科尔曼", "机械工业出版社", 2023, "第4版", None, "TP301.6/11"),
    ("WEB-1", "计算机网络", "谢希仁", "电子工业出版社", 2021, "第8版", None, "TP393/08"),
    ("DB-1", "数据库系统概论", "王珊、萨师煊", "高等教育出版社", 2023, "第6版", None, "TP311.13/06"),
    ("SE-1", "软件工程", "伊恩·萨默维尔", "机械工业出版社", 2021, "第10版", None, "TP311.5/10"),
    ("OS-1", "现代操作系统", "安德鲁·塔嫩鲍姆", "机械工业出版社", 2024, "第5版", None, "TP316/05"),
]


def floor_document():
    nodes = [
        ("entry", "主入口", "entrance", 12, 77), ("west", "西侧通道", "junction", 29, 77),
        ("center", "中央通道", "junction", 50, 77), ("east", "东侧通道", "junction", 77, 77),
        ("north", "北侧通道", "junction", 50, 21), ("service", "服务台", "service", 12, 51),
        ("face01a", "A01-A", "shelf", 39, 37), ("face01b", "A01-B", "shelf", 20, 37),
        ("face02a", "A02-A", "shelf", 39, 61), ("face02b", "A02-B", "shelf", 20, 61),
        ("face03a", "A03-A", "shelf", 61, 37), ("face03b", "A03-B", "shelf", 82, 37),
    ]
    edges = [("entry", "west"), ("entry", "service"), ("west", "center"), ("center", "east"),
             ("center", "north"), ("center", "face02a"), ("north", "face01a"),
             ("west", "face02b"), ("face02b", "face01b"), ("north", "face03a"), ("east", "face03b")]
    faces = [(node, label, "东" if label.endswith("A") else "西")
             for node, label, kind, _, _ in nodes if kind == "shelf"]
    return FloorMap.model_validate({
        "name": "一层 · 开架借阅区", "service_label": "主入口旁服务台",
        "nodes": [dict(id=id_, label=label, kind=kind, x=x, y=y) for id_, label, kind, x, y in nodes],
        "edges": [dict(a=a, b=b) for a, b in edges],
        "faces": [dict(id=label, label=label, shelf=label.split("-")[0], side=label[-1],
                       node_id=node, direction=direction) for node, label, direction in faces],
        "blocks": [{"x": 24, "y": 28, "width": 11, "height": 17, "label": "A01"},
                   {"x": 24, "y": 52, "width": 11, "height": 16, "label": "A02"},
                   {"x": 66, "y": 28, "width": 11, "height": 17, "label": "A03"},
                   {"x": 66, "y": 52, "width": 11, "height": 16, "label": "阅览区"}],
    }).model_dump()


def seed(connection):
    if connection.execute(select(models.c.version).where(models.c.library_id == LIBRARY)).first():
        return
    stamp = now()
    for number, (edition_id, title, author, publisher, year, volume, isbn, call) in enumerate(BOOKS, 1):
        connection.execute(editions.insert().values(library_id=LIBRARY, edition_id=edition_id, title=title,
                           author=author, publisher=publisher, year=year, volume=volume, isbn=isbn))
        connection.execute(holdings.insert().values(
            library_id=LIBRARY, holding_id=f"H{number:02}", edition_id=edition_id, barcode=f"DIW{number:06}",
            call_number=call, status="loaned" if edition_id == "OS-1" else "available", synced_at=stamp,
        ))
    connection.execute(holdings.insert().values(library_id=LIBRARY, holding_id="H09", edition_id="DS-3",
                       barcode="DIW000009", call_number=BOOKS[0][-1], status="available", synced_at=stamp))
    document = floor_document()
    values = dict(library_id=LIBRARY, floor=FLOOR, revision=1, document=document, actor="demo-seed", updated_at=stamp)
    connection.execute(maps.insert().values(**values, slot="published"))
    connection.execute(map_history.insert().values(**values))
    connection.execute(models.insert().values(library_id=LIBRARY, version=BASELINE["version"],
                       document=BASELINE, actor="demo-seed", published_at=stamp))
