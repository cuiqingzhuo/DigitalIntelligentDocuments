# 数智文枢

面向单馆、单楼层开架区的找书工具。读者检索书目并选定版本和架面，按路线到达后拍照，依据静态书脊框取书，再扫码核对馆藏。馆员可以维护书目、地图和异常工单，独立核验员可以冻结试验答案并记录首拿与最终结果。

项目需求及现场门槛见 [项目开发与验收说明](数智文枢-项目开发与验收.md)。工程验证记录见 [验收记录](reports/验收记录.md)。

## 本机演示

需要 Python 3.11 或更新版本。在项目根目录执行以下命令。

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- [读者端](http://127.0.0.1:8000/) 搜索「数据结构」，选第 3 版，选择 A01-A，在到架确认中输入 A01-A。选择「目标在画面内」并运行识别。第 3 版演示条码为 `DIW000001` 和 `DIW000009`，第 2 版为 `DIW000002`。
- [馆员工作台](http://127.0.0.1:8000/workbench) 提供馆员、管理员、独立核验员三个演示入口。读者上报异常后可切换到馆员接单与关闭，再回读者端刷新回执。
- [接口文档](http://127.0.0.1:8000/docs) 可以直接查看请求结构。创建会话、识别、核验与反馈的请求使用 `Idempotency-Key` 请求头。

本机演示使用 SQLite，将数据库和可选留存照片保存在 `runtime/`。演示书目、条码和底图是自建数据。三张识别样例是生成的图像，点击识别会运行实际的检测和 OCR。网页版使用服务器识别自己拍摄的照片，上传前须勾选本次授权。安卓端默认本机处理，在线时可单独选择上传复核。

## Android

安卓读者端支持相机、相册、架标和馆藏条码扫描，断网时使用已缓存的试点书目、路网和包内 OCR，反馈与核验按队列重传。界面内可切换大字模式。[安装包与校验信息](release/README.md) · [构建说明](mobile/README.md)

Android 模拟器默认使用 `http://10.0.2.2:8000`。真机在同一局域网时，在项目根目录执行以下命令。

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

在 App 右上角设置中填入电脑的局域网地址，例如 `http://192.168.1.10:8000`。可将 [书架样例](web/samples/target.jpg) 传入相册验证本机识别。实际试点使用 HTTPS 地址和馆内实物条码。

## PostgreSQL 部署

使用 Docker Compose 启动 PostgreSQL 和 FastAPI。

```powershell
Copy-Item .env.example .env
docker compose up --build
```

也可以在已有 PostgreSQL 上部署。创建应用数据库后，设置 `DIW_DATABASE_URL=postgresql+psycopg://用户:密码@主机:5432/数据库`，启动时自动建表。数据库和照片存储应配置持久化卷，服务前配置 HTTPS 入口。Docker 默认绑定本机，需接入内网时由部署方设置 `DIW_BIND_ADDRESS`。

| 环境变量 | 用途 |
|---|---|
| `DIW_DEMO` | 本机默认 `1`。正式单馆试点使用 `0`，启动时要求已配置工作台身份 |
| `DIW_TOKENS_JSON` | JSON 对象，键为各工作台令牌，值含 `actor`、`role`、`library`。角色为 `librarian`、`admin`、`verifier`。演示模式关闭后令牌至少 32 字符 |
| `DIW_PUBLIC_LIBRARY`、`DIW_PUBLIC_FLOOR` | 读者匿名入口对应的馆和楼层标识。工作台写入范围取自登录身份的馆标识 |
| `DIW_CLOUD_REVIEW` | 设为 `0` 可关闭图片上传和云复核 |
| `DIW_PHOTO_RETENTION_DAYS` | 默认 `0`，识别后不落盘。正数只用于获准上传的图片，有效上限 30 天，服务器定期清理 |
| `DIW_STALE_HOURS` | 可借状态提示陈旧的时限，默认 72 小时 |
| `DIW_WITH_OBB` | 在 Compose 构建中设为 `1` 以安装可选的旋转框模型运行依赖 |

工作台令牌 JSON 的单身份形如 `{"<长随机令牌>":{"actor":"馆员甲","role":"librarian","library":"campus"}}`。各个角色用不同令牌。导入页提供两个 UTF-8 CSV 模板。馆员预览错误行后可提交有效数据。版本和复本以馆内 ID 去重，较早的复本同步时间不会改写较新状态。地图在工作台上传底图、标出入口、架面方向与路径后保存草稿，管理员发布后才对读者可见。

## 数据与模型

- 按需求文档第 5 节复核数据。原始视觉数据、NCL 中文书目和 Gutenberg 电子书目录的许可、来源与校验信息保存在 `data/`。
- `python -m pip install -r requirements-train.txt` 安装训练依赖。运行 `python -m scripts.train_matcher` 拟合中文题名重排器，写出权重和分区完整索引的 Top‑1／Top‑3 结果。
- 复现视觉训练时，从作者发布页下载原包 `BookSpineDataset_v1.0.zip` 到 `data/visual/`。运行 `python scripts/visual/bookspines.py --export bookspine_yolo` 先校验原包并生成 OBB 数据，再运行 `python -m scripts.vision.train_obb`。命令默认用 CUDA，可通过 `--device cpu` 切换。训练得到的权重和留出集指标分别在 `models/` 与 `reports/`。管理员可发布指定的权重文件，默认演示使用字符串基线。[BookSpineDataset v1.0 上游发布](https://github.com/wyn2282145606-crypto/BookSpineDataset/releases/tag/v1.0)
- `python -m scripts.vision.evaluate_ocr` 用辅助整图转写集计算字符错误率。检测采用 YOLO 旋转框标注与评分方式。[Ultralytics OBB 文档](https://docs.ultralytics.com/tasks/obb) · [真实书架数据许可](data/visual/SOURCE-LICENSE.txt)

已发布的服务器模型条目冻结阈值、重排权重和检测模型校验值。安卓包内推理有独立版本号。现场验证前使用馆内实拍和本馆目录校准，首拿验收采用合作馆的独立实体核验记录。

## 现场核验和导出

1. 用独立核验员身份录入并冻结任务。记录匿名读者号、方法、允许复本、目标可见性和难例类型，必要时录入人工标注四点框。答案仅供核验端访问。
2. 展示任务卡时记 0 秒。可按「检索与选版、到架、拍照、反馈」记录步骤。生成 App 会话后填入读者端的会话号。
3. 每次实际取下书时记下经过秒数和条码。无条码的版本任务可以填写两个核验人和实际版本。首拿保留，重试正确单列为最终成功。提前放弃和超时在计时比较中都按 120 秒处理。
4. 从工作台导出逐任务 CSV 和带识别事件的 JSON。经授权留存的识别照片可通过核验接口检查。运行 `python -m scripts.trial_report trial-results.json --sus sus.csv --output reports/pilot-metrics.json` 汇总指标。SUS CSV 表头为 `participant_code,q1,q2,q3,q4,q5,q6,q7,q8,q9,q10`。

## 复现工程检查

```powershell
python -m pip install pytest httpx playwright
python -m pytest -q
python -m scripts.browser_smoke --browser "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --evidence reports/screens
```

浏览器脚本使用独立的临时数据库，验证查书、相邻架面确认、识别、错版重试、离线反馈、馆员回执、地图发布和二维码。
