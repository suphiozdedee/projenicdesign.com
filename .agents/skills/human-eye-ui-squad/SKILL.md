---
name: human-eye-ui-squad
description: Multi-agent UI design review & quality squad that inspects designs with a human eye, weeding out amateurism, mid-level mediocrity, and generic AI templates.
metadata:
  category: multi-agent-orchestration
  triggers: human eye audit, design review squad, ui audit, anti amateurism gate, agent squad
---

# Human-Eye UI Review & Quality Squad

This skill orchestrates a dedicated squad of 4 specialized AI agents to continuously audit, critique, and perfect user interfaces. The squad operates with a **ruthless human-eye standard** to ensure no amateurism or mid-level AI template tropes reach production.

---

## The Squad Roster

| Agent Name | Specialty & Phase | Core Focus & Anti-Pattern Elimination |
|---|---|---|
| 👁️ **Human-Eye Auditor** | Quality Gate / Audit | Eliminates pixel misalignment, mismatched radii, amateur spacing, and cheap drop shadows. |
| 🎨 **Creative Director** | Concept & Art Direction | Eliminates generic font defaults, boring colors, and robotic AI templates. |
| 🏛️ **UI Architect** | Layout Geometry | Enforces Bento Grids, Double-Bezel card shells, and mobile responsive collapse. |
| ✨ **Motion Choreographer** | Animation & Kinetics | Enforces custom cubic-bezier transitions, button kinetic physics, and GPU safety. |

---

## 🔄 Multi-Agent Review Protocol

```
+--------------------------+
|  1. Creative Director    | --> Establishes Art Direction & Vibe
+--------------------------+
             |
             v
+--------------------------+
|   2. UI Architect        | --> Constructs Bento Layouts & Double Bezel Cards
+--------------------------+
             |
             v
+--------------------------+
|  3. Motion Choreographer | --> Injects Cubic-Bezier Kinetics & Hover Physics
+--------------------------+
             |
             v
+--------------------------+
|  4. Human-Eye Auditor    | --> RUTHLESS AUDIT GATE (REJECT if amateur/mid-level)
+--------------------------+
             |
             +---> APPROVED? --> Final Production Code
             |
             +---> REJECTED? --> Cycles back to Architect/Engineer with exact fix diffs
```

---

## Human-Eye Quality Gates

1. **Concentric Geometry Law:** $\text{Inner Radius} = \text{Outer Radius} - \text{Padding}$.
2. **Dynamic Tracking:** Negative letter-spacing for titles, wide tracking for micro-tags.
3. **Button Sizing:** Strict `32px`/`40px`/`48px` matrix with button-in-button trailing icon badges.
4. **Hairline Depth:** 1px semi-transparent borders with top inset highlights instead of dark shadows.
5. **Spatial Rhythm:** Generous section padding ($\ge 80\text{px}$) to let the design breathe.
