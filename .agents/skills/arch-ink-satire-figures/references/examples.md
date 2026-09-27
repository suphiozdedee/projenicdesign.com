# 转译示例

以下三个示例演示「主题 → 决策表 → 成品提示词」的完整转译。**这些是转译示范，不是已出图验证的成品**——风格内核来自 SKILL.md 的锁死基座，出图效果以实际生成结果为准。

## 示例一：单人劳作

**主题**：街边剃头匠给客人修面，客人仰头眯眼。

**决策表**

| 决策项 | 选择 |
|---|---|
| 情节类型 | 单人动作 / 劳作 |
| 核心动作 | 剃头匠一手扶头一手举剃刀修面 |
| 人物配置与朝向 | 主人物剃头匠 + 次要人物客人（两人，动作关系必需）；剃头匠朝右，客人坐姿仰头朝右略后仰 |
| 轮廓性格 | 剃头匠：瘦长微驼；客人：圆胖 |
| 接触点 | 剃头匠左手扶住客人后脑，右手剃刀贴住客人脸颊 |
| 道具 | 一件：客人肩上的围布；剃刀算手部延伸 |
| 黑块 | 两人头发、鞋子、张开的嘴内部 |

**成品提示词**

```text
Use case: article or social-media illustration, vertical 3:4.

Scene: flat opaque pure white paper background filling all four corners of the frame, completely blank; no environment, no architecture, no furniture, no landscape, no decorative patterns, no background figures; if the action needs context, keep only one or two instantly recognizable props directly tied to the core action, nothing else; five to eight short, loose black ground strokes of slightly uneven length directly under the figures' feet to suggest the ground.

Subject: a thin, slightly hunched street barber in strict right-facing profile, standing behind a round chubby seated customer whose head tilts back with eyes squeezed shut; the barber's left hand supports the back of the customer's head, his right hand presses a straight razor against the customer's upturned cheek; a plain apron cloth drapes over the customer's shoulders; the customer's head leans slightly to the right and up, body relaxed, feet flat on the ground; the barber leans in with his back curved forward; one dominant action only: the shaving stroke.

Key details: traditional Chinese lianhuanhua (pocket comic-book) style, single-line ink drawing; figures built from a few large continuous contour lines, with slightly oversized heads, simplified torsos and simplified limbs; each line drawn like one confident pass of a black brush or dip pen — outer silhouettes use medium-thick, clear, forceful black lines, while facial features, clothing folds and prop interiors use slightly thinner lines; lines are fluid and economical with a slight natural wobble and hand-drawn irregularity, never re-traced, never sketchy search lines, never smooth mechanical vector curves; edges stay sharp and crisp with no softening, no ink bleeding, no texture erosion; strict black-and-white: pure black ink lines, a few solid black fills and the pure white paper — no grey, no colour, no gradients, no shading, no hatching, no cross-hatching, no screentone dots, no grain, no paper texture, no brush noise, no ageing effects; faces are highly abstract and symbolic — the profile eye is a single short arc or a small black dot, the eyebrow a single short arc, the nose a simple hooked profile line, the mouth one short curve or a simple black-and-white shape, the ear carries only one or two summary lines inside; no realistic eyeballs, no eye bags, no teeth, no gums, no heavy wrinkles, no nostril detail, no skin texture, no sweat drops, no facial muscles; clothing is loose, generalized, mostly white, with at most one to three fold lines per sleeve, torso or trouser leg that only explain the direction of the action; no fabric realism, no dense cloth wrinkles, no stitching, no lighting, no volume on clothes; hands are simplified lianhuanhua symbols — the palm reads as one shape, fingers as a few short arcs that read the grip, support, push, pull, point or carry of the action; no fingernails, no knuckles, no palm lines, no tendons, no fingers fusing together, no palm floating next to a prop; solid black fills reserved for hair, shoes, the inside of an open mouth or one key visual anchor, while clothing and large props stay mostly white; mood: naive, humorous, mildly exaggerated, everyday-life folk satire.

Composition: 3:4 vertical; flat eye-level side observation, no bird's-eye view, no worm's-eye view, no wide-angle distortion, no dramatic perspective, no strong foreground-background scaling; the main figure or interacting group centered, occupying about 65% to 85% of the frame width and 75% to 88% of the frame height, with extra white space in front of the figure in the direction of the action; full-body framing — head, both hands, key props, contact points and both feet fully inside the frame, never cropped at wrists, fingers, elbows, knees, ankles or mid-prop; for wide two-figure interactions, rope pulling, carrying or long props the figures may sit slightly smaller, but the action relationship stays instantly readable and the white margins around all four sides stay generous.

Text in image: no text, no letters, no numbers, no symbols anywhere in the image.

Constraints: no text, letters, numbers or symbols anywhere in the image; no modern western caricature grimaces, no realistic portraits, no real human anatomy, no muscle lines, no detailed facial anatomy, no dense wrinkles, no sweat drops, no explosion symbols, no dense action lines; no frontal or three-quarter faces — strict 90-degree side profiles only, each face shows exactly one eye, one eyebrow and one ear, never a second eye, a second brow or the other ear; no photographs, no 3D rendering, no anime, no manga, no western superhero comics, no fashion illustration, no smooth vector art, no pencil shading, no watercolour, no oil painting, no realistic sketching; no greyscale shading, no volume lighting, no gradients, no screentones, no hatching, no cross-hatching, no paper grain, no scan noise, no ageing texture; no dense interior detailing, no repeated outlines, no mechanical prop details that clash with the figure's line style; no complex backgrounds, no architecture, no furniture arrangements, no landscapes, no decorative borders, no titles, no captions, no speech bubbles, no text labels, no signatures, no seals, no logos, no watermarks; no transparent background — the background must be fully opaque, even pure white; no unrelated figures, no extra props, no extra arms, no extra hands, no extra fingers, no fused limbs, no floating props, no broken ropes, no ambiguous contact; no barber chair, no mirror, no shop interior, no towel cabinet.
```

## 示例二：双人对抗

**主题**：两个人争夺一根绳子，各据一端往后拽。

**决策表**

| 决策项 | 选择 |
|---|---|
| 情节类型 | 双人对抗 / 拉扯 |
| 核心动作 | 两人反向拉绳，绳子绷直 |
| 人物配置与朝向 | 两人相向：左人朝右、右人朝左，身体都向后倾斜 |
| 轮廓性格 | 左：矮小敦实；右：瘦长前倾 |
| 接触点 | 两人双手都握住绳子，绳在两双手之间绷成一条直线 |
| 道具 | 一件：绷直的绳子 |
| 黑块 | 两人头发、鞋子 |

**成品提示词**

```text
Use case: article or social-media illustration, vertical 3:4.

Scene: flat opaque pure white paper background filling all four corners of the frame, completely blank; no environment, no architecture, no furniture, no landscape, no decorative patterns, no background figures; if the action needs context, keep only one or two instantly recognizable props directly tied to the core action, nothing else; five to eight short, loose black ground strokes of slightly uneven length directly under the figures' feet to suggest the ground.

Subject: a short stocky man in strict right-facing profile and a tall thin man in strict left-facing profile, standing opposite each other, tugging at the two ends of one rope; the rope runs perfectly taut and straight between their two hands, each man leaning his whole body backward away from the other with his back foot braced and his front leg bent; both of their hands grip the rope — the left figure holds it in front of his chest, the right figure holds it in front of his chest; the rope is a single unbroken line from hand to hand; one dominant action only: the tug-of-war pull.

Key details: traditional Chinese lianhuanhua (pocket comic-book) style, single-line ink drawing; figures built from a few large continuous contour lines, with slightly oversized heads, simplified torsos and simplified limbs; each line drawn like one confident pass of a black brush or dip pen — outer silhouettes use medium-thick, clear, forceful black lines, while facial features, clothing folds and prop interiors use slightly thinner lines; lines are fluid and economical with a slight natural wobble and hand-drawn irregularity, never re-traced, never sketchy search lines, never smooth mechanical vector curves; edges stay sharp and crisp with no softening, no ink bleeding, no texture erosion; strict black-and-white: pure black ink lines, a few solid black fills and the pure white paper — no grey, no colour, no gradients, no shading, no hatching, no cross-hatching, no screentone dots, no grain, no paper texture, no brush noise, no ageing effects; faces are highly abstract and symbolic — the profile eye is a single short arc or a small black dot, the eyebrow a single short arc, the nose a simple hooked profile line, the mouth one short curve or a simple black-and-white shape, the ear carries only one or two summary lines inside; no realistic eyeballs, no eye bags, no teeth, no gums, no heavy wrinkles, no nostril detail, no skin texture, no sweat drops, no facial muscles; clothing is loose, generalized, mostly white, with at most one to three fold lines per sleeve, torso or trouser leg that only explain the direction of the action; no fabric realism, no dense cloth wrinkles, no stitching, no lighting, no volume on clothes; hands are simplified lianhuanhua symbols — the palm reads as one shape, fingers as a few short arcs that read the grip, support, push, pull, point or carry of the action; no fingernails, no knuckles, no palm lines, no tendons, no fingers fusing together, no palm floating next to a prop; solid black fills reserved for hair, shoes, the inside of an open mouth or one key visual anchor, while clothing and large props stay mostly white; mood: naive, humorous, mildly exaggerated, everyday-life folk satire.

Composition: 3:4 vertical; flat eye-level side observation, no bird's-eye view, no worm's-eye view, no wide-angle distortion, no dramatic perspective, no strong foreground-background scaling; the main figure or interacting group centered, occupying about 65% to 85% of the frame width and 75% to 88% of the frame height, with extra white space in front of the figure in the direction of the action; full-body framing — head, both hands, key props, contact points and both feet fully inside the frame, never cropped at wrists, fingers, elbows, knees, ankles or mid-prop; for wide two-figure interactions, rope pulling, carrying or long props the figures may sit slightly smaller, but the action relationship stays instantly readable and the white margins around all four sides stay generous.

Text in image: no text, no letters, no numbers, no symbols anywhere in the image.

Constraints: no text, letters, numbers or symbols anywhere in the image; no modern western caricature grimaces, no realistic portraits, no real human anatomy, no muscle lines, no detailed facial anatomy, no dense wrinkles, no sweat drops, no explosion symbols, no dense action lines; no frontal or three-quarter faces — strict 90-degree side profiles only, each face shows exactly one eye, one eyebrow and one ear, never a second eye, a second brow or the other ear; no photographs, no 3D rendering, no anime, no manga, no western superhero comics, no fashion illustration, no smooth vector art, no pencil shading, no watercolour, no oil painting, no realistic sketching; no greyscale shading, no volume lighting, no gradients, no screentones, no hatching, no cross-hatching, no paper grain, no scan noise, no ageing texture; no dense interior detailing, no repeated outlines, no mechanical prop details that clash with the figure's line style; no complex backgrounds, no architecture, no furniture arrangements, no landscapes, no decorative borders, no titles, no captions, no speech bubbles, no text labels, no signatures, no seals, no logos, no watermarks; no transparent background — the background must be fully opaque, even pure white; no unrelated figures, no extra props, no extra arms, no extra hands, no extra fingers, no fused limbs, no floating props, no broken ropes, no ambiguous contact; no slack in the rope, no knot detail, no flag, no referee, no boundary line on the ground.
```

## 示例三：人物与动物

**主题**：驼背老汉拄着拐杖，弯腰把碗里的食撒给围在脚边的鸡。

**决策表**

| 决策项 | 选择 |
|---|---|
| 情节类型 | 人物与动物 |
| 核心动作 | 老汉一手扶拐一手撒食 |
| 人物配置与朝向 | 老汉朝右，一只鸡在右前方仰头接食 |
| 轮廓性格 | 老汉：驼背；鸡：矮小 |
| 接触点 | 老汉左手拄拐、右手端碗向下倾倒；鸡的喙对着撒落的食 |
| 道具 | 两件：拐杖、碗 |
| 黑块 | 老汉头发、鞋子、鸡的尾羽 |

**成品提示词**

```text
Use case: article or social-media illustration, vertical 3:4.

Scene: flat opaque pure white paper background filling all four corners of the frame, completely blank; no environment, no architecture, no furniture, no landscape, no decorative patterns, no background figures; if the action needs context, keep only one or two instantly recognizable props directly tied to the core action, nothing else; five to eight short, loose black ground strokes of slightly uneven length directly under the figures' feet to suggest the ground.

Subject: a hunched old man in strict right-facing profile, his spine curving forward in one big arc, leaning on a walking stick held in his left hand, his right hand tipping a bowl downward; a single chicken in strict right-facing profile stands in front of him with its beak lifted toward the falling feed; the chicken's head reaches toward the bowl, the old man's gaze points down at the chicken, the stick touches the ground beside his front foot; one dominant action only: the scattering of feed.

Key details: traditional Chinese lianhuanhua (pocket comic-book) style, single-line ink drawing; figures built from a few large continuous contour lines, with slightly oversized heads, simplified torsos and simplified limbs; each line drawn like one confident pass of a black brush or dip pen — outer silhouettes use medium-thick, clear, forceful black lines, while facial features, clothing folds and prop interiors use slightly thinner lines; lines are fluid and economical with a slight natural wobble and hand-drawn irregularity, never re-traced, never sketchy search lines, never smooth mechanical vector curves; edges stay sharp and crisp with no softening, no ink bleeding, no texture erosion; strict black-and-white: pure black ink lines, a few solid black fills and the pure white paper — no grey, no colour, no gradients, no shading, no hatching, no cross-hatching, no screentone dots, no grain, no paper texture, no brush noise, no ageing effects; faces are highly abstract and symbolic — the profile eye is a single short arc or a small black dot, the eyebrow a single short arc, the nose a simple hooked profile line, the mouth one short curve or a simple black-and-white shape, the ear carries only one or two summary lines inside; no realistic eyeballs, no eye bags, no teeth, no gums, no heavy wrinkles, no nostril detail, no skin texture, no sweat drops, no facial muscles; clothing is loose, generalized, mostly white, with at most one to three fold lines per sleeve, torso or trouser leg that only explain the direction of the action; no fabric realism, no dense cloth wrinkles, no stitching, no lighting, no volume on clothes; hands are simplified lianhuanhua symbols — the palm reads as one shape, fingers as a few short arcs that read the grip, support, push, pull, point or carry of the action; no fingernails, no knuckles, no palm lines, no tendons, no fingers fusing together, no palm floating next to a prop; solid black fills reserved for hair, shoes, the inside of an open mouth or one key visual anchor, while clothing and large props stay mostly white; mood: naive, humorous, mildly exaggerated, everyday-life folk satire.

Composition: 3:4 vertical; flat eye-level side observation, no bird's-eye view, no worm's-eye view, no wide-angle distortion, no dramatic perspective, no strong foreground-background scaling; the main figure or interacting group centered, occupying about 65% to 85% of the frame width and 75% to 88% of the frame height, with extra white space in front of the figure in the direction of the action; full-body framing — head, both hands, key props, contact points and both feet fully inside the frame, never cropped at wrists, fingers, elbows, knees, ankles or mid-prop; for wide two-figure interactions, rope pulling, carrying or long props the figures may sit slightly smaller, but the action relationship stays instantly readable and the white margins around all four sides stay generous.

Text in image: no text, no letters, no numbers, no symbols anywhere in the image.

Constraints: no text, letters, numbers or symbols anywhere in the image; no modern western caricature grimaces, no realistic portraits, no real human anatomy, no muscle lines, no detailed facial anatomy, no dense wrinkles, no sweat drops, no explosion symbols, no dense action lines; no frontal or three-quarter faces — strict 90-degree side profiles only, each face shows exactly one eye, one eyebrow and one ear, never a second eye, a second brow or the other ear; no photographs, no 3D rendering, no anime, no manga, no western superhero comics, no fashion illustration, no smooth vector art, no pencil shading, no watercolour, no oil painting, no realistic sketching; no greyscale shading, no volume lighting, no gradients, no screentones, no hatching, no cross-hatching, no paper grain, no scan noise, no ageing texture; no dense interior detailing, no repeated outlines, no mechanical prop details that clash with the figure's line style; no complex backgrounds, no architecture, no furniture arrangements, no landscapes, no decorative borders, no titles, no captions, no speech bubbles, no text labels, no signatures, no seals, no logos, no watermarks; no transparent background — the background must be fully opaque, even pure white; no unrelated figures, no extra props, no extra arms, no extra hands, no extra fingers, no fused limbs, no floating props, no broken ropes, no ambiguous contact; no extra chickens, no courtyard, no fence, no doorway, no basket, no scattered grain piles on the ground.
```

## 成组产出

成组时每张按同一套决策表走，四段基座逐条重复。同一组内至少保证三项不同：情节、轮廓性格、道具或黑块。五张全是「老汉驼背拄拐」即使换了动作也算失败。
