# 编译契约（消歧义铁律）

每次输出前必须满足。这是「成品」和「拼装件」的分界：成品不留任何选项、菜单、占位符。

## 5 条铁律

1. **每个槽位解析成唯一值**：删掉所有 `/` 选项和 `<>` 占位。成品里不允许出现 `sphere / cube / block` 这类菜单。
2. **正文零中文**：提示词正文只能出现英文美术指导；唯一例外是「要渲染进画面的标题文字」。任何中文注释一律不留——模型会把它画进画面。
3. **单锚点 + 单受光**：全画面只有一个黑色锚点质量，只有一处受光。第二个平权主体、第二处打亮，一律不写。
4. **否定只进 Constraints 段**：正文用正向描述，所有 `no / never` 收进末尾 Constraints。**不允许输出独立的 `Negative prompt:` 块**——目标模型没有 negative 通道。
5. **文字精确且只有两行**：只渲染 descriptor 与 headline，必带 verbatim 条款。不允许模型自行补字、缩写、翻译或加装饰标点。

## 锁死的风格内核（LOCKED，每条必带）

无论什么主体、哪个语言寄存器，这几段逐字照抄，不改写不精简：

**Scene 基座**
```
a white void enclosed by a faint one-point perspective wireframe room: hairline pale grey grid lines rule the floor, the ceiling and both side walls, every line converging on a single vanishing point at the centre of the frame, the grid cells widening toward the frame edges; the grid is a pale backdrop drawn in the lightest grey, sitting far behind the subject; no other environment.
```

**Key details 基座**
```
strict black-and-white palette with extreme tonal purity — the background reads as clean paper white, the black anchor reads as solid ink black, the backdrop grid is the palest hairline grey, and grey appears nowhere else except as a narrow transition on the subject's own surfaces, never as a field tone or a wash over the white; clean studio product-photography lighting with one soft directional key light; one clear black anchor mass carries the composition while everything else dissolves toward white or into black rather than competing for light; hard clean edges throughout; photorealistic rendering; mood: minimal, rational, quietly surreal, premium editorial.
```

**Composition 基座**
```
5:2 horizontal banner; headline centered in the upper 30% of the frame, sitting in a clear band of plain white that the grid lines stop short of; the subject occupies the lower half and stands in front of the grid; at least half of the total image area stays empty clean white.
```

**Constraints 基座**
```
no overall grey cast, no mottled grey noise, no film grain, no colour of any kind; the perspective grid stays hairline pale grey, never darkening toward black, never thickening, and never reading as a solid field; no grid lines crossing the headline or the descriptor; no additional text, labels, logos or watermarks; no second competing subject and no scattered props; the subject never overlaps or occludes the text.
```

按主体追加的排除项（例：`the block stays solid black and is never lifted to grey`）接在基座后面，不替换基座。

## 四条影调铁律（本风格的承重墙）

这四条是把「还行」推到「高级」的全部差距所在，缺一即回落：

1. **极端影调纯度**：不能只写 black and white，要写 `extreme tonal purity` 并按名封堵已知失败模式——`no mottled grey noise, no film grain`。斑驳灰噪点是这套风格最常见的塌方。
2. **单一受光锚点**：明写「只有 X 受光，其余沉入黑/白」。让整排结构都亮着，黑区会被切碎成灰色柱廊，画面立刻廉价。
3. **文字平面实黑**：`flat solid-black graphic type with no shadow, no bevel, no perspective`。大标题一旦带阴影或立体感，编辑感全丢。
4. **网格是衬底不是内容**：透视网格只负责把空白撑出纵深，永远是最浅的一档灰发丝线。一旦加粗、压深或糊成一片，它就从衬底变成第二个主体，把黑锚点的唯一性直接吃掉。网格与标题之间必须留净空——线穿过标题，编辑感就没了。

## 两语言寄存器（TEXT 段按此替换）

两个寄存器的**所有渲染文字都是衬线体**，这是本风格的身份特征，不给无衬线选项。

**EN（默认）**
```
Text in image: two lines only, centered, rendered as flat solid-black graphic type with no shadow, no bevel, no perspective. Line 1: "<DESCRIPTOR>" in small, widely letterspaced uppercase serif. Line 2: "<HEADLINE>" in very large high-contrast Didone serif with extreme thick-thin stroke contrast and hairline serifs, the first visual focus; size ratio about 1:6. Render all text verbatim, no extra words.
```

**CN**
```
Text in image: two lines only, centered, rendered as flat solid-black graphic type with no shadow, no bevel, no perspective. Line 1: "<DESCRIPTOR>" in a small, widely letterspaced high-contrast Chinese serif (思源宋体 / Source Han Serif). Line 2: "<HEADLINE>" in a very large high-contrast Chinese serif (思源宋体 / Source Han Serif) with thin horizontal strokes, thick vertical strokes and sharp triangular serifs, the first visual focus; size ratio about 1:5. Render every Chinese character exactly as given, with correct and complete strokes; no invented, malformed, simplified-into-nonsense or extra glyphs.
```

CN 寄存器的 Constraints 基座末尾追加：`no malformed or invented Chinese characters, no Latin text anywhere in the image.`

## 中文渲染专项

CN 和 EN 是对等的两个寄存器，中文标题按下面这套走：

- **点名字族**：`高对比中文衬线（思源宋体 / Source Han Serif）` + 横细竖粗 + 尖锐三角衬角。只写 `Song/Ming serif` 字形稳定性明显低一档。**不要给中文写 `Didone`**——那是拉丁字体谱系名，中文没有对应血统，写了等于没写。
- **字数压到最短**：主标题 ≤8 字，descriptor ≤6 字。这是版式要求（超宽横幅上部只有 30% 的带宽），不是规避风险。
- **quality 必须 high**，size `3200×1280`。中文字形密度高，低分辨率下必糊。
- **出图后逐字核对**：有错字、缺笔、造字，**原样重生成**——模型无跨次记忆，重抽即换一批字形，多出两张挑一张就行。常用字和复杂字都能稳定生成，不预设乱码，也不要退回「留白 + 后期排字」。
- 中英混排不做。一张图只用一个语言寄存器。

## 固定字段顺序

成品提示词永远按此七段顺序输出，带段落标签：

```
Use case → Scene → Subject → Key details → Composition → Text in image → Constraints
```

## 成品骨架（填好即发，无占位符）

```text
Use case: article cover banner, ultra-wide 5:2 editorial format.

Scene: <Scene 基座逐字照抄；主体需要不同地面时，只把 floor 那一项换成点阵或其他处理，墙面与顶面的网格不动>

Subject: <一个装置的完整描述——叫得出名字的实物 + 状态 + 明确指出哪一块是纯黑锚点 + 投影>

Key details: <Key details 基座逐字照抄> <可选：本主体的锐点点名，如 sharp points to preserve: the extended fingertip, the sphere's edge>

Composition: <Composition 基座逐字照抄> <可选：本主体的位置微调，如 slightly right of center, seen at a low three-quarter angle>

Text in image: <按 EN 或 CN 寄存器整段替换，填入 descriptor 与 headline>

Constraints: <Constraints 基座逐字照抄><按主体追加的排除项>
```

## 出图参数（不进正文）

- size `3200×1280`（5:2；长边 <3840、双边 16 的倍数、比例 ≤3:1、总像素 655,360–8,294,400）
- quality `high`

参数层信息绝不写进提示词正文。

## 迭代纪律

- **一次只改一处**。这套风格的质感维度互相联动，凭措辞猜因果已实测出过错。
- **效果不对时先删修饰再加修饰**。修饰词过多会互相竞争。
- **已验证有效的提示词不为了合规去改**。回退到最近的已知良好版本是合法且常常最优的动作。
- 措辞已到天花板时**换工具而不是继续堆词**：后期拉黑白场（十秒确定性解决影调）、编辑模式定点改（`Change only X. Keep everything else exactly the same.`）、同提示词多抽几张再选。
