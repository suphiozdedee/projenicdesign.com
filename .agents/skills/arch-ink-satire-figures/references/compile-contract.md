# 编译契约（消歧义铁律）

每次输出前必须满足。这是「成品」和「拼装件」的分界：成品不留任何选项、菜单、占位符。

## 5 条铁律

1. **每个槽位解析成唯一值**：删掉所有 `/` 选项和 `<>` 占位。成品里不允许出现 `fat / thin / hunched` 这类菜单。
2. **正文零中文**：提示词正文只能出现英文美术指导。任何中文注释一律不留——模型会把它画进画面。
3. **一张一情节**：全画面只有一个核心动作或一个核心关系。第二个平权情节一律不写，拆成下一张。
4. **否定只进 Constraints 段**：正文用正向描述，所有 `no / never` 收进末尾 Constraints。**不允许输出独立的 `Negative prompt:` 块**——目标模型没有 negative 通道。
5. **画面零文字**：Text in image 段固定写 `no text...`，Constraints 基座里已带 `no text`，不必重复追加。

## 锁死的风格内核（LOCKED，每条必带）

无论什么主题，下面四段逐字照抄，不改写不精简。唯一可变项：Composition 基座开头的画幅（默认 `3:4 vertical`），跟随用户指定的比例替换，其余不动。

**Scene 基座**

```
flat opaque pure white paper background filling all four corners of the frame, completely blank; no environment, no architecture, no furniture, no landscape, no decorative patterns, no background figures; if the action needs context, keep only one or two instantly recognizable props directly tied to the core action, nothing else; five to eight short, loose black ground strokes of slightly uneven length directly under the figures' feet to suggest the ground.
```

**Key details 基座**

```
traditional Chinese lianhuanhua (pocket comic-book) style, single-line ink drawing; figures built from a few large continuous contour lines, with slightly oversized heads, simplified torsos and simplified limbs; each line drawn like one confident pass of a black brush or dip pen — outer silhouettes use medium-thick, clear, forceful black lines, while facial features, clothing folds and prop interiors use slightly thinner lines; lines are fluid and economical with a slight natural wobble and hand-drawn irregularity, never re-traced, never sketchy search lines, never smooth mechanical vector curves; edges stay sharp and crisp with no softening, no ink bleeding, no texture erosion; strict black-and-white: pure black ink lines, a few solid black fills and the pure white paper — no grey, no colour, no gradients, no shading, no hatching, no cross-hatching, no screentone dots, no grain, no paper texture, no brush noise, no ageing effects; faces are highly abstract and symbolic — the profile eye is a single short arc or a small black dot, the eyebrow a single short arc, the nose a simple hooked profile line, the mouth one short curve or a simple black-and-white shape, the ear carries only one or two summary lines inside; no realistic eyeballs, no eye bags, no teeth, no gums, no heavy wrinkles, no nostril detail, no skin texture, no sweat drops, no facial muscles; clothing is loose, generalized, mostly white, with at most one to three fold lines per sleeve, torso or trouser leg that only explain the direction of the action; no fabric realism, no dense cloth wrinkles, no stitching, no lighting, no volume on clothes; hands are simplified lianhuanhua symbols — the palm reads as one shape, fingers as a few short arcs that read the grip, support, push, pull, point or carry of the action; no fingernails, no knuckles, no palm lines, no tendons, no fingers fusing together, no palm floating next to a prop; solid black fills reserved for hair, shoes, the inside of an open mouth or one key visual anchor, while clothing and large props stay mostly white; mood: naive, humorous, mildly exaggerated, everyday-life folk satire.
```

**Composition 基座**

```
3:4 vertical; flat eye-level side observation, no bird's-eye view, no worm's-eye view, no wide-angle distortion, no dramatic perspective, no strong foreground-background scaling; the main figure or interacting group centered, occupying about 65% to 85% of the frame width and 75% to 88% of the frame height, with extra white space in front of the figure in the direction of the action; full-body framing — head, both hands, key props, contact points and both feet fully inside the frame, never cropped at wrists, fingers, elbows, knees, ankles or mid-prop; for wide two-figure interactions, rope pulling, carrying or long props the figures may sit slightly smaller, but the action relationship stays instantly readable and the white margins around all four sides stay generous.
```

**Constraints 基座**

```
no text, letters, numbers or symbols anywhere in the image; no modern western caricature grimaces, no realistic portraits, no real human anatomy, no muscle lines, no detailed facial anatomy, no dense wrinkles, no sweat drops, no explosion symbols, no dense action lines; no frontal or three-quarter faces — strict 90-degree side profiles only, each face shows exactly one eye, one eyebrow and one ear, never a second eye, a second brow or the other ear; no photographs, no 3D rendering, no anime, no manga, no western superhero comics, no fashion illustration, no smooth vector art, no pencil shading, no watercolour, no oil painting, no realistic sketching; no greyscale shading, no volume lighting, no gradients, no screentones, no hatching, no cross-hatching, no paper grain, no scan noise, no ageing texture; no dense interior detailing, no repeated outlines, no mechanical prop details that clash with the figure's line style; no complex backgrounds, no architecture, no furniture arrangements, no landscapes, no decorative borders, no titles, no captions, no speech bubbles, no text labels, no signatures, no seals, no logos, no watermarks; no transparent background — the background must be fully opaque, even pure white; no unrelated figures, no extra props, no extra arms, no extra hands, no extra fingers, no fused limbs, no floating props, no broken ropes, no ambiguous contact.
```

## Subject 段的写法要求

主题只进这一段，其余六段按骨架走。按顺序写清：

1. **人物身份起手**：一个能叫出名字的具体身份（街边剃头匠、拉纤的船工、挑担货郎），不是一个抽象概念。
2. **核心动作**：一句话、一个动词。全图只有这一个动作。
3. **人物数量与朝向**：一至两名人物（只在主题需要时加一只动物或一个关键道具）；严格 90 度侧视，同向或相向由情节定。
4. **轮廓性格**：圆胖 / 瘦长 / 驼背 / 挺直 / 矮小，写进人物描述；先剪影后细节。
5. **接触点**：哪只手、碰哪件东西、接触位置在哪。递交、搀扶、推拉、争夺、共同搬运必须把接触写实。
6. **道具**：≤两件，一眼叫名，只留识别轮廓。
7. **黑块点名**：纯黑实心给头发 / 鞋子 / 张开的嘴内部 / 一处重心，至少点一处。

## 固定字段顺序

成品提示词永远按此七段顺序输出，带段落标签：

```
Use case → Scene → Subject → Key details → Composition → Text in image → Constraints
```

## 成品骨架（填好即发，无占位符）

```text
Use case: article or social-media illustration, vertical 3:4.

Scene: <Scene 基座逐字照抄>

Subject: <按 Subject 写法要求完成的单情节人物描述>

Key details: <Key details 基座逐字照抄>

Composition: <Composition 基座逐字照抄，开头画幅跟随比例设置>

Text in image: no text, no letters, no numbers, no symbols anywhere in the image.

Constraints: <Constraints 基座逐字照抄><按本主题追加的排除项，如本主题特有的无关物>
```

## 出图参数（不进正文）

- 默认画幅 3:4，size `1536×2048`，quality `high`。
- 1:1 用 `2048×2048`；4:3 用 `2048×1536`；其他比例按等价高度取整，quality 一律 `high`。
