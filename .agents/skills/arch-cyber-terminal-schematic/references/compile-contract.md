# 编译契约（消歧义铁律）

每次输出前必须满足。这是「成品」和「拼装件」的分界：成品不留任何选项、菜单、占位符。

## 7 条铁律

1. **每个槽位解析成唯一值**：删掉所有 `/` 选项和 `{}` 占位。成品里不允许出现 `green / cyan / amber` 这类菜单。
2. **正文零中文**：提示词正文只能出现英文美术指导；例外只有两处——「要渲染进画面的标题文字」和「要渲染进画面的中文标签」，两者都必须放在引号里、写在 TEXT 段。任何中文注释一律不留——模型会把它画进海报。字体名（思源黑体 / Source Han Sans）不算注释，可以出现在 TYPOGRAPHY 段。
3. **单一色相**：全图只有一种霓虹绿，绝不并列第二套配色。屏幕质感层也受这条管——CRT 做单色荧光屏，不许出现 RGB 子像素三联点、色散分离、彩虹边，那会把单色打破。
4. **否定只进 AVOID**：正文用正向描述，所有 `no / not / avoid` 收进末尾 AVOID 块。
5. **主标题精确，次级 micro-text 受控**：主标题必须精确渲染（如 `从 Loop 到 Graph`、`GRAPH`），放进引号里写，并补一句 `render verbatim, no extra words or characters`。次级小字分两层，规矩不同，见下面第 7 条。一律禁：URL、假品牌名、长段落、二维码、真代码块。
   ⚠️ **日期禁，时钟时间不禁**。禁的是年份、`YYYY-MM-DD`、月份名——那些会被模型随机编造，且把图钉死在一个时间点上。`[10:14:23]` 这种 HH:MM:SS 时钟不属于日期，它是这套风格日志块最有效的部件，可以放心用。两者写进 AVOID 时也要分开写：`no dates or years`，不要写成笼统的 `no timestamps`。
6. **中文一律不写 monospace**：等宽只管拉丁字。中文没有等宽体的常规概念，对中文写 monospace 是无效指令；所有中文——主标题和图面标签一视同仁——走 `heavy geometric sans（黑体）`，按需也可点名思源黑体这类字族。
7. **两层文字，两套规矩**：
   - **HUD 层全拉丁**：角落面板、图例、状态行、角标一律短拉丁字段（PROJECT / TYPE / VERSION / NODE / EDGE / FLOW / SYS READY / LINK ACTIVE / [ok]），tiny、锁网格、有功能感。这层不开中文口子——中英混在同一个部件里显脏。
   - **图面层可中文**：节点标签、条目名、轴标、行标可用短中文，**2-6 字、粗黑体、全图 ≤8 个**。超过 8 个说明这张图该拆成两张。中文标签必须在 TEXT 段里**逐个列出**并要求 verbatim，不列就等着模型造假字。仍禁中文长句和中文段落。

## 锁死的风格内核（LOCKED，每条必带）

无论封面还是插图，这几条恒定：

- 主视觉是**一张仪表图面**，八族选一族（见「图面族几何」一节），全族共用同一套线稿语法：细描轮廓、均匀细线、线上有发光小圆点当数据流；
- 一层 **HUD 仪表框**，按预设装配（见「HUD 预设」一节），元素池是：角落数据面板（PROJECT / TYPE / VERSION 类短标签）、图例（key NODE / EDGE / FLOW）、边缘标尺带刻度、底部系统状态行（短拉丁字段，可带 HH:MM:SS 时钟）、角标；
- **等宽字体**贯穿全图的拉丁字；所有中文（标题和图面标签）走粗黑体（思源黑体 / Source Han Sans, Heavy）；
- **画的内容是平面线稿**，无照片景深、无写实材质、无渐变填充（发光辉光除外）；
- **示意图线稿全部一样粗**——这是屏幕的语法，线型分级（粗轮廓 / 细尺寸线 / 点划中心线）属于图纸，不进这里。**注意作用域：这条只管线稿，不管字**。标题是排版不是线条，允许也应该用粗字重；AVOID 里也必须写成 `varying stroke weights inside the linework`，不能写成笼统的 `varying line weights`，否则会把标题字重一起否掉；
- 画的内容之上盖一层 **SCREEN TEXTURE**，见下节，这层是必带的，不是可选装饰；
- 层级靠尺度和位置，不靠装饰花纹。

「平面线稿」管的是被画出来的图，「屏幕质感」管的是承载它的那块屏。两者不冲突，写提示词时也不要混在同一句里。

## 唯一寄存器预设（LOCKED 段照抄）

```
LOCKED — pure black background that lifts into a faint dark halo around anything lit; a single neon-green phosphor color for every line, node, glyph and label; all schematic linework shares one uniform thin stroke weight; soft outer glow on lit elements; monospace typeface for all Latin text; the drawn content is flat line-art with no photographic depth and no gradient fill.
QUALITY — crisp phosphor CRT terminal render shot straight off a real screen, sharp monospace holding up through the scanline texture, precise alignment, subtle bloom.
```

## HUD 预设（开篇选一次，篇内锁死）

HUD 不是一套固定装置。开篇选一个预设，**同一篇的封面和所有插图共用同一个**；跨篇随意换——篇内一致保证成系列，跨篇可变保证十篇文章的图不长成一个样。

**HUD-FULL** — 角落面板 + 图例 + 边缘标尺 + 状态行 + 角标。给流程/工程题材的拓扑族封面。
```
HUD FRAME — top-left metadata block (short mono labels like PROJECT / TYPE / VERSION); top-right legend keying NODE / EDGE / FLOW; a bottom row of short system-status fields (SYS READY / LINK ACTIVE / [ok]); thin dimension rulers with tick marks along an edge; corner crop marks.
```

**HUD-LEAN（默认）** — 角落面板 + 状态行 + 角标。给大多数情况，以及信息密度高或标题很长时。
```
HUD FRAME — top-left metadata block (short mono labels like PROJECT / TYPE / VERSION); a bottom row of short system-status fields (SYS READY / LINK ACTIVE / [ok]); corner crop marks.
```

**HUD-EDGE** — 只角标 + 一条边标尺。给自带坐标轴的族。
```
HUD FRAME — a single thin dimension ruler with tick marks along one edge; corner crop marks; nothing else.
```

**双重刻度是图面扩容后最容易出的脏。** BAR RANK / TIME SERIES / MATRIX GRID / TIMELINE 这四族图面自己带轴和刻度，再套 HUD-FULL 的边缘标尺，四周会出现两套读不通的尺子。这四族默认 HUD-EDGE 或 HUD-LEAN，绝不 HUD-FULL。

想自定义组合也行——从元素池里挑，但整篇锁死同一套，且同族避让规则照样生效。

## 屏幕质感层（SCREEN TEXTURE）

这是这套风格「像不像真东西」的分水岭。没有这一层，出来的是干净的矢量流程图；有了这一层，出来的是一块正在发光的老显像管屏幕。

**一句话原则：质感盖在内容之上，永远不吃内容。** 扫描线压过标题和节点标签，但压不糊——缩略图状态下所有字仍然认得出来。

三档强度，一次选一档，整篇统一。用户没指定用 **CRT-02**。

**CRT-01 CLEAN（轻）** — 信息密度高的插图、或标题很长的封面
```
SCREEN TEXTURE — the whole frame is the glowing face of a monochrome CRT monitor. Fine horizontal scanlines run edge to edge at even spacing, dark and low in contrast, riding over every element including the title. Lit strokes bleed a little light into the scanlines beside them. A gentle vignette darkens the four corners. Every glyph stays sharp and fully legible through the texture.
```

**CRT-02 STANDARD（默认）** — 封面和大多数插图
```
SCREEN TEXTURE — the whole frame is a close photograph of a monochrome phosphor CRT monitor. Fine horizontal scanlines run edge to edge at even spacing, dark and low in contrast, riding over every element including the title, and their brightness varies subtly from band to band as if the tube is mid-refresh. Under them sits a much finer vertical aperture-grille striping, visible only inside lit areas as phosphor grain. Lit strokes bloom and halate into the scanlines around them, brightest where two lines cross. The picture bows outward in gentle barrel curvature, falls off into a soft vignette at the four corners, and defocuses very slightly at the extreme edges. A faint even haze of glass lies over the whole surface. Every glyph stays sharp and fully legible through the texture.
```

**CRT-03 DEGRADED（重）** — 只用在封面或单张大图，讲「旧 / 遗留 / 故障 / 归档」时
```
SCREEN TEXTURE — the whole frame is a close photograph of a tired old monochrome phosphor CRT monitor. Fine horizontal scanlines run edge to edge at even spacing, dark and low in contrast, riding over every element including the title, their brightness pulsing subtly band to band. Under them sits a much finer vertical aperture-grille striping, visible inside lit areas as phosphor grain. One wide, very faint horizontal refresh band drifts across the picture, lifting whatever it passes over. Bright strokes bloom, halate, and trail a short horizontal ghost smear to their right. A barely-visible burn-in ghost of an older diagram lingers underneath. The picture bows outward in barrel curvature, darkens into heavy corner vignette, defocuses at the extreme edges, and carries a fine dusting of analog static across dusty, aged glass. Every glyph stays sharp and fully legible through the decay — the wear stays under the content, never on top of it.
```

配套要求（选了任一档都要同步）：

- **AVOID 必须换掉旧的 `photographic texture`**——那句会把整层质感否掉。改成禁「画里出现照片实物 / 景深」，见下文骨架。
- **AVOID 必带三条新禁令**：RGB 色散分离与彩虹边（破单色）、扫描线粗到把字咬碎、画面里出现显示器边框外壳和桌面（屏幕面要铺满整幅）。
- **QUALITY 用上面那段新版**，里面已经写了「字要穿过扫描线仍然锐利」。

想要纸的质感（纸纹、折痕、水渍、图钉孔）不在这里改——那是 `engineering-blueprint-sheet` 的 PRINT TEXTURE，整套语法都不同，转过去用。

## 固定字段顺序

成品提示词永远按此顺序。质感层排在 TEXT 之后、AVOID 之前——它是盖在画完的成品上的一道后期，不是画的内容：

```
STYLE ANCHOR  →  FORMAT(锁死画幅)  →  LOCKED  →  [LAYOUT(带大标题的封面必带)]
→  DOMINANT VISUAL(一族图面)  →  HUD FRAME(按预设)  →  TYPOGRAPHY  →  TEXT(要渲染的文字, 声明一次)
→  SCREEN TEXTURE(按强度档)  →  AVOID  →  QUALITY
```

## 成品骨架（填好即发，无占位符）

```text
STYLE ANCHOR — a technical schematic screen rendered as a phosphor CRT terminal: an engineering <topology diagram / bar chart / plot / matrix / stack / timeline / metric readout> drawn in glowing monospace, like a system-monitor HUD.

A <5:2 landscape / 16:9> poster.

LOCKED — <粘贴 LOCKED 段>

DOMINANT VISUAL — <粘贴选中图面族的几何句，名词按内容替换>

HUD FRAME — <粘贴选中的 HUD 预设>

TYPOGRAPHY — monospace for all Latin text; <有中文时: Chinese labels in a heavy Chinese sans (思源黑体 / Source Han Sans, Heavy)>; <封面: the main title set very large across the center, glowing / 插图: only tiny field labels, no large title>.

TEXT — main title must render verbatim, with no extra words or characters added: "<主标题>". <封面可加一行小副标>. <有中文标签时逐个列出: The following labels must also render verbatim: "<标签1>", "<标签2>", "<标签3>".> Remaining labels are short Latin field tags only; no years or calendar dates, URLs, fake brands, long paragraphs, QR codes, or real code.

SCREEN TEXTURE — <粘贴选中的 CRT 强度档>

AVOID — any hue beyond the single neon green; blueprint blue; RGB colour split, chromatic aberration or rainbow subpixel fringing; scanlines heavy or wide enough to break up the glyphs; a visible monitor bezel, plastic housing, desk or room around the screen; paper texture, fold creases or stains; varying stroke weights inside the linework, dimension arrows or a ruled title block; two competing sets of tick-marked scales; photographic objects or depth of field inside the artwork itself; 3D bevels; hard specular glare sitting over the content; decorative icons outside the schematic; garbled or invented Chinese characters; dense unreadable code walls.

QUALITY — <粘贴 QUALITY 段>
```

## 封面标题图文分离铁律（防止字和图糊在一起）

封面的巨型标题和图面同处一块画面、又同为一个绿相，最容易糊成一团。凡是带大标题的封面，必带下面四条，缺一即改：

1. **留净空 + 线绕行**：中央横向留一条净空带给标题；图面的线从标题的上方和下方两道弧线绕过去，**绝不从标题正后方穿过**（`edges never cross behind the title`，写进 LAYOUT，并进 AVOID）。条形和堆叠这类占满中带的族，做法是整个图面下移或分列两侧，不是让标题压上去。
2. **明度分层**：单色不等于单一亮度。标题用近白亮绿 + 强辉光（`bright near-white green, heavy bloom`），图面线压暗成中绿（`dimmer mid-green`）。这是最有效的图文分离手段，且不破坏单色。
3. **字重分层**：标题粗重（`heavy bold weight`），示意线细（`all schematic linework shares one uniform thin stroke weight`）。⚠️ 这条要求标题粗，所以 AVOID 里的线宽禁令**必须限定作用域**写成 `varying stroke weights inside the linework`；写成笼统的 `varying line weights` 会把标题字重一起否掉，模型只能二选一。同理 AVOID 不许出现 `heavy display poster type`——那和「标题要大要粗」是正面冲突。
4. **净空带 knockout**：标题所在的横带保持无线条穿过（`a clear horizontal band reserved for the headline, with no linework crossing it`）。⚠️ **不要写「黑底板 / knockout plate」**——底已经是纯黑，画一块黑板要么完全看不见，要么被画成一个灰色矩形或带边框的标题框，还和 AVOID 里的 `ruled title block` 打架。要的是线主动避让形成的空，不是额外画出来的面。同时 HUD 面板 / 状态行 / 标尺整体压暗，不与标题争亮度。

插图（16:9）通常没有大标题、只有拉丁小标签，不受本节约束；一旦某张插图要放大标题，同样套这四条。

**加了屏幕质感层之后还有一条**：辉光和扫描线是两个方向相反的力——bloom 让标题往外糊，scanline 让标题被横向切碎，两个叠在一起最先牺牲的就是最大的那几个字。所以 SCREEN TEXTURE 段末尾那句 `every glyph stays sharp and fully legible through the texture` 是硬性的，不许删；标题笔画本来就细的字（细体、纤细中文）在这套风格里直接不用。

## 招牌装置一致性（批量成系列的关键）

一致性是**篇内**要求，不是跨篇要求。分清三档：

**篇内常量**（同一篇文章的封面 + 所有插图必须逐字相同）：

1. **同一个 HUD 预设**——开篇选定，之后一字不改；
2. **同一档 SCREEN TEXTURE**——整篇同一强度，一字不改地复制粘贴。封面 CRT-03、插图 CRT-01 会当场散架，一张像旧屏幕一张像新屏幕，缩略图并排一眼假。真有某张插图信息太密扛不住质感，做法是**整篇一起降档**，不是单张降；
3. **同一套线稿语法**——细描轮廓、等粗细线、连线发光点，八族都照这套画。

**篇内变量**（本来就该逐图变）：中央图面的族和结构、封面主标题、各图的标签。**一篇里混族是允许的**——封面拓扑、插图条形，只要 HUD 和质感一致，缩略图并排还是一眼同系列。

⚠️ 混族有一条避让：一篇里既有自带轴的族（条形/时序/矩阵/时间轴）又有不带轴的族，**整篇统一退到 HUD-LEAN**。HUD-LEAN 无标尺，两边都不打架。这跟质感「整篇一起降档」是同一个逻辑——不一致的解法永远是整篇降，不是单张改。

**跨篇自由**：HUD 预设、质感档、主用族，下一篇可以整套换。锁死的只有寄存器（黑底 / 单绿 / 等宽 / 辉光）和铁律。

出图后并排缩略图检查：同一篇一眼是同一系列，不同篇一眼不是同一张。

## 图面族几何（把段落逻辑转成图）

八族，**一张图只用一族**。下面每族给一句可直接填进 `DOMINANT VISUAL` 的英文几何，按内容替换里面的名词即可。

### 1. TOPOLOGY 节点拓扑（默认招牌）

管流程、分工、依赖、循环、分诊。族内还有一张子词汇表：

| 段落逻辑 | 对应拓扑 |
|---|---|
| 一个 Agent 反复干（Loop） | 单节点 + 一条自循环回环箭头 |
| 组队分工（Graph 总览） | ENTRY → fan-out 到多个具名节点 → MERGE → EXIT 的完整菱形 |
| 有先后 vs 能并行（假依赖） | 上排一条 A→B→C→D 线性链（标注 chain），下排断开的假边 + 展开成并行分支 |
| Fan-out / Fan-in 菱形 | 一入口散射到 N 节点，再收束到一个汇合节点 |
| 验收 / 循环回退 | 菱形末端一个 verifier 节点，一条虚线回环箭头指回上游，带 stop 标记 |
| Router 分诊 / 判断清单 | 一个菱形判断节点分出两条边（light path / heavy path）或四个 checkbox 门 |

```
DOMINANT VISUAL — one node-and-edge topology, flat line-art: <入口 / fan-out / 并行节点 / 汇合 / 出口 / 回环里实际用到的那些>; nodes are thin-stroked circles with tiny line-icons, edges are thin lines carrying small glowing dots.
```

### 2. BAR RANK 条形排行

管排序、占比、谁多谁少、前后对比。

```
DOMINANT VISUAL — a horizontal bar chart of <N> rows, flat line-art: each row is a thin-outlined rectangle filled with an even lattice of small squares whose count encodes the value, bars sorted longest to shortest, a label sitting flush left of each bar and a short numeric readout flush right; one thin baseline axis with tick marks runs under the bars.
```

坑：条形的填充**用方块点阵，不用实心块**——实心大色块在单绿荧光里会糊成一片光斑，且违反 flat line-art。自带轴，走 HUD-EDGE。

### 3. TIME SERIES 时序折线

管趋势、增长、波动、拐点。

```
DOMINANT VISUAL — a single time-series line plot, flat line-art: one thin polyline traversing left to right across a faint dotted grid, small glowing dots marking each data point, one point marked with a thin crosshair and a short callout label, thin axes with tick marks along the bottom and left edges.
```

坑：只画**一条**线。两条以上在单色里无法区分（没有第二个颜色可用），要对比就改用 SPLIT COMPARE 出两格。自带轴，走 HUD-EDGE。

### 4. MATRIX GRID 矩阵网格

管二维分类、覆盖度、密度、有无。

```
DOMINANT VISUAL — a matrix grid of <行>×<列> cells, flat line-art: thin uniform gridlines, each cell either empty or holding a small glyph (filled dot, cross, or check) to encode state, short row labels down the left and short column labels across the top, a small legend keying the three cell states.
```

坑：格子状态**最多三种**（空 / 点 / 叉），四种以上单色分不开。自带轴，走 HUD-EDGE。

### 5. STACK 层级堆叠

管上下游、抽象层、技术栈、构成。

```
DOMINANT VISUAL — a vertical stack of <N> horizontal bands, flat line-art: each band a thin-outlined rectangle spanning most of the width, stacked with even gaps, a label centered inside each band and a short Latin tier tag flush right, thin vertical connector ticks joining adjacent bands.
```

坑：层数 3-6 之间。band 里**不填色**，只放字。

### 6. TIMELINE 时间轴

管阶段、里程碑、演进顺序。

```
DOMINANT VISUAL — one horizontal timeline axis running edge to edge, flat line-art: evenly spaced tick marks along it, <N> milestone nodes as thin-stroked circles sitting on the axis, each with a short label on a thin leader line alternating above and below the axis, a small glowing dot marking the current position.
```

坑：milestone 上**不写年份**（撞禁日期铁律），用阶段名或 01/02/03。自带轴，走 HUD-EDGE。

### 7. BIG METRIC 巨型指标

管单一数字砸脸。

```
DOMINANT VISUAL — one enormous numeric readout dominating the center, flat line-art: the figure set in very large glowing monospace with a small unit tag beside it, a short caption label beneath, and one small sparkline of a thin polyline sitting under the caption; a thin bracket frame around the whole readout block.
```

坑：**数字也要 verbatim**，写进 TEXT 段加引号，否则模型会改数。这族和封面大标题争亮度，插图专用，别放封面。

### 8. SPLIT COMPARE 左右对照

管 A vs B、旧法 vs 新法、错 vs 对。

```
DOMINANT VISUAL — a split diagram divided by one thin vertical rule: on the left <左侧几何>, tiny label <左标>; on the right <右侧几何>, tiny label <右标>; both halves drawn at the same scale in thin-stroked flat line-art.
```

坑：两侧几何**必须同族同尺度**（都是拓扑，或都是条形），不然不是对照是拼贴。这族本身是个容器，里面装的还是前七族之一。

### 表外结构怎么办

表是常见解，不是全集。表外可以自造，但必须同时满足三条：

1. **一个主结构**——能指出画面里唯一的那个中心几何；
2. **一句话说清**——写不出一句英文几何描述的，模型也画不出来；
3. **缩略图可读**——缩到封面尺寸结构还认得出。

三条缺一，退回最近的一族。自造的图面同样受所有铁律管：单色、等粗线稿、flat line-art、HUD 预设、质感层。

## 和 engineering-blueprint-sheet 的边界

两个 skill 长得像，但载体不同，元素不能串：

| | cyber-terminal-schematic（屏） | engineering-blueprint-sheet（纸） |
|---|---|---|
| 线 | 全部等粗细 | 分级：粗轮廓 / 细尺寸 / 点划中心 / 虚线隐藏 |
| 光 | 辉光、bloom、halation | 无。印上去的墨 |
| 质感 | 扫描线、荧光颗粒、屏幕弧度、烧屏 | 纸纹、折痕、水渍、图钉孔 |
| 框 | HUD 面板 + 图例 + 系统状态行 | 图框分区 + 标题栏 + 修订表 |
| 字 | 等宽字 | 制图技术字 |

用户要「终端 / HUD / 赛博 / 扫描线」走这个；要「蓝图 / 图纸 / 三视图 / 爆炸图 / 标题栏」转过去。两个都想要就出两张，不要合成一张。
