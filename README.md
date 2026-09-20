# Archive Quote Alignment API

档案引文定位服务：给定原文与一组有序引文，返回每条引文在原文中的字符起止偏移；除精确匹配外，还支持允许少量编辑的模糊定位、由 2～5 个有序片段组成的节录定位，以及在多份文档之间为引文选择归属文档。

## 启动

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**（端口 **8000**），新旧接口均通过该端口访问。

- 健康检查：`GET http://localhost:8000/health`
- 交互式文档：`http://localhost:8000/docs`

## API

### `POST /quotes/align`（精确匹配，行为保持不变）

请求体：

```json
{
  "text": "Archive  text\nwith repeats. Archive text.",
  "quotes": ["Archive text", "repeats", "Archive text", "absent"]
}
```

响应（`results` 与 `quotes` 一一对应，无法定位的引文为 `null`）：

```json
{
  "results": [
    {"start": 0, "end": 13},
    {"start": 19, "end": 26},
    {"start": 28, "end": 40},
    null
  ]
}
```

`start` / `end` 为**原始文本**的字符偏移（`end` 为开区间），可直接用于 `text[start:end]` 切片。

### `POST /quotes/align-fuzzy`（模糊匹配）

每条引文包含 `text` 与 `max_edits`（允许的最大编辑距离，取值 **0～2**）。

请求体：

```json
{
  "text": "Archive  text\nwith repeats. Archive text.",
  "quotes": [
    {"text": "Archlve text", "max_edits": 1},
    {"text": "repeets", "max_edits": 2},
    {"text": "Archive text", "max_edits": 0},
    {"text": "zzz", "max_edits": 0}
  ]
}
```

响应（多返回一个 `edit_distance`，表示所选片段与引文的实际编辑距离；无法定位为 `null`）：

```json
{
  "results": [
    {"start": 0, "end": 13, "edit_distance": 1},
    {"start": 19, "end": 26, "edit_distance": 1},
    {"start": 28, "end": 40, "edit_distance": 0},
    null
  ]
}
```

### `POST /quotes/align-excerpts`（有序节录匹配）

每条节录（excerpt）由 **2～5 个非空白** `fragments` 与 `max_gap`（相邻片段在**原文**中允许的最大间隔字符数，取值 **0～10000**）组成。片段按精确匹配（空白折叠）定位；一条节录命中要求全部片段按数组顺序依次出现、互不重叠，且相邻片段在原文中的间隔不超过 `max_gap`。片段数为 1 或超过 5、含空白片段、`max_gap` 越界或缺失时返回 422。

请求体：

```json
{
  "text": "Archive  text\nwith repeats. Archive text.",
  "excerpts": [
    {"fragments": ["Archive text", "repeats"], "max_gap": 20},
    {"fragments": ["Archive text", "repeats"], "max_gap": 0},
    {"fragments": ["absent", "text"], "max_gap": 100}
  ]
}
```

响应（`results` 与 `excerpts` 一一对应；命中项给出整条节录的外层 `start`/`end`、每个片段的 `fragment_spans`，以及相邻片段之间被跳过的非空原文区间 `omitted_ranges`；未命中为 `null`）：

```json
{
  "results": [
    {
      "start": 0,
      "end": 26,
      "fragment_spans": [
        {"start": 0, "end": 13},
        {"start": 19, "end": 26}
      ],
      "omitted_ranges": [
        {"start": 13, "end": 19}
      ]
    },
    null,
    null
  ]
}
```

第二条节录要求 `max_gap` 为 0（片段必须相邻），而唯一可行的片段组合间隔为 6 个字符，故未命中；第三条含未出现的片段，同样未命中。

### `POST /quotes/attribute`（多文档归属）

请求包含 **2～4 份**文档（`id` 必须唯一，否则返回 422）与一组有序引文；每条引文包含 `text` 与 `max_edits`（**0～2**）。服务为每条引文联合选择**至多一个**「文档 + 原始偏移」归属：同一文档内已选片段按引文顺序排列且互不重叠，不同文档之间互不影响。

请求体：

```json
{
  "documents": [
    {"id": "doc-a", "text": "Archive  text\nwith repeats."},
    {"id": "doc-b", "text": "Archive text. Final text."}
  ],
  "quotes": [
    {"text": "Archlve text", "max_edits": 1},
    {"text": "repeats", "max_edits": 0},
    {"text": "Final text", "max_edits": 0},
    {"text": "zzz", "max_edits": 0}
  ]
}
```

响应（`results` 与 `quotes` 一一对应；命中项给出 `document_id` 与**该文档原始文本**的偏移及实际编辑距离，未定位为 `null`）：

```json
{
  "results": [
    {"document_id": "doc-a", "start": 0, "end": 13, "edit_distance": 1},
    {"document_id": "doc-a", "start": 19, "end": 26, "edit_distance": 0},
    {"document_id": "doc-b", "start": 14, "end": 24, "edit_distance": 0},
    null
  ]
}
```

### `GET /health`

返回 `{"status": "ok"}`。

## 匹配规则

- **空白折叠**：匹配时先将原文与引文中的连续空白（空格、换行、制表符等）折叠为单个空格；返回的偏移始终指向原始文本的原始字符。
- **模糊编辑距离**（`/quotes/align-fuzzy` 与 `/quotes/attribute`）：在空白折叠之后，以 **Unicode 字符（码位）** 为单位计算 Levenshtein 距离，插入、删除、替换均计为 1；枚举归一化原文中所有距离不超过该引文 `max_edits`（0～2）的非空片段。
- **重复引文**：同一引文出现多次时，各次匹配互不重叠，且保持引文数组的顺序。
- **联合选择**（`/quotes/align` 与 `/quotes/align-fuzzy`）：从各引文的候选位置中整体选出一组互不重叠、保持数组顺序的定位，目标依次为：
  1. 最大化成功定位的引文条数；
  2. 最小化总编辑距离（精确匹配恒为 0；仅模糊接口涉及）；
  3. 最小化已定位片段之间跳过的原文字符总数（首段之前、末段之后不计）；
  4. 仍并列时取偏移序列字典序最小（最早）的一组。
- **多文档归属**（仅 `/quotes/attribute`）：为每条引文从全部文档的模糊候选中整体选出至多一个「文档 + 偏移」，同一文档内的已选片段须按引文顺序且不重叠，目标依次为：
  1. 最大化成功定位的引文条数；
  2. 最小化总编辑距离；
  3. 仍并列时取（文档序号、起始、结束）序列字典序最小的一组（文档序号按 `documents` 数组顺序，从 0 开始）。
- **节录链接**（仅 `/quotes/align-excerpts`）：每条节录的片段须按数组顺序依次命中、互不重叠（允许相邻），且相邻片段在**原文**中的间隔字符数不超过 `max_gap`（0～10000）；`omitted_ranges` 为相邻片段之间被跳过的非空原文区间（片段相邻时该列表为空）。
- **节录联合选择**（仅 `/quotes/align-excerpts`）：先为每条节录枚举所有可行片段组合，再跨节录整体选出一组互不重叠、保持数组顺序的定位，目标依次为：
  1. 最大化成功定位的节录条数；
  2. 最小化已定位节录内部省略的原文字符总数（即各节录 `omitted_ranges` 长度之和）；
  3. 最小化已定位节录之间跳过的原文字符总数（首条之前、末条之后不计）；
  4. 仍并列时取偏移序列字典序最小（最早）的一组。
- 空引文或纯空白引文视为无法定位，返回 `null`（节录接口的空白片段则直接被拒绝，返回 422）。

## 本地开发（可选）

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest            # 运行测试
```

## 项目结构

```
app/
  align.py   # 归一化、候选生成（精确与模糊）、节录片段组合枚举、联合选择与多文档归属（动态规划）
  main.py    # FastAPI 路由与请求/响应模型
tests/       # pytest 测试
Dockerfile
docker-compose.yml
```
