/*
 * Procedural cherry blossom tree.
 *
 * Ported from the React original to vanilla SVG, with one substantive change:
 * the original always renders a finished tree, but here the tree has to *grow*
 * across the seven stages as commitments are kept.
 *
 * The growth is done by building the full tree once and then revealing it by
 * depth, rather than rebuilding per stage. That matters because the PRNG is
 * consumed in recursion order: rebuilding with a shallower depth would change
 * the random sequence and the trunk would visibly jump between stages. Built
 * once, the lower structure is identical at every stage and the tree simply
 * extends outward.
 */

const MAX_DEPTH = 8;
const GROW_TIME = 1.15;

// Blossoms attach to every branch from this depth outward, but only the
// current outermost band is displayed -- so a young tree carries a few buds at
// its tips rather than none at all. Kept at 1 so that stage 0, "First bud",
// actually has one.
const BLOSSOM_FROM_DEPTH = 1;
const DRIFTER_COUNT = 22;

const SVG_NS = "http://www.w3.org/2000/svg";

/* Deterministic PRNG, so the user's tree is the same tree every morning. */
function makeRandom(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function buildTree(seed) {
  const rand = makeRandom(seed);
  const branches = [];

  const grow = (x, y, angle, length, width, depth, startTime) => {
    // Curve each segment slightly so limbs read as organic, not geometric.
    const bend = (rand() - 0.5) * (depth < 2 ? 0.9 : 0.42);
    const midAngle = angle + bend * 0.5;
    const cx = x + Math.cos(midAngle) * length * 0.55;
    const cy = y + Math.sin(midAngle) * length * 0.55;
    const ex = x + Math.cos(angle + bend) * length;
    const ey = y + Math.sin(angle + bend) * length;

    const duration = GROW_TIME * (0.45 + length / 150);
    const endTime = startTime + duration;

    const blossoms = [];
    if (depth >= BLOSSOM_FROM_DEPTH) {
      const count = 2 + Math.floor(rand() * 2);
      for (let i = 0; i < count; i++) {
        blossoms.push({
          x: ex + (rand() - 0.5) * 34,
          y: ey + (rand() - 0.5) * 30,
          r: 4.6 + rand() * 3.4,
          rot: rand() * 360,
          delay: endTime + rand() * 1.2,
          tone: Math.floor(rand() * 3),
        });
      }
    }

    branches.push({
      d:
        "M " + x.toFixed(2) + " " + y.toFixed(2) +
        " Q " + cx.toFixed(2) + " " + cy.toFixed(2) +
        " " + ex.toFixed(2) + " " + ey.toFixed(2),
      width,
      depth,
      delay: startTime,
      duration,
      blossoms,
      // Kept so the viewBox can be fitted to whatever is actually visible.
      points: [[x, y], [cx, cy], [ex, ey]],
    });

    if (depth >= MAX_DEPTH || length < 9) return;

    // The lower trunk splits into main limbs; the upper structure forks more.
    const forks = depth < 2 ? 2 : rand() < 0.28 ? 3 : 2;
    const spread = depth < 2 ? 0.34 + rand() * 0.12 : 0.6 + rand() * 0.38;

    for (let i = 0; i < forks; i++) {
      const offset = (i / (forks - 1) - 0.5) * 2 * spread + (rand() - 0.5) * 0.22;
      grow(
        ex,
        ey,
        angle + bend + offset,
        length * (0.68 + rand() * 0.16),
        Math.max(0.55, width * (0.62 + rand() * 0.12)),
        depth + 1,
        endTime - duration * 0.08
      );
    }
  };

  grow(0, 0, -Math.PI / 2 + 0.04, 180, 32, 0, 0);
  return branches;
}

/* How much tree each stage reveals. Stage 0 is a sapling, stage 6 the full
 * canopy; the names in the UI ("First bud" … "Ancient sakura") track this. */
function depthForStage(stage, stageCount) {
  const span = MAX_DEPTH - 2;
  const ratio = stageCount > 1 ? stage / (stageCount - 1) : 1;
  return Math.round(2 + span * ratio);
}

/* Proportions, expressed as fractions of the tree's own span.
 *
 * The source geometry was authored against a fixed 1040-unit viewBox showing a
 * finished tree. Fitting the frame to each stage means those absolute numbers
 * no longer hold: in a tight frame a 32-unit trunk reads as 6% of the width
 * (stubby) while a 6-unit blossom reads as under 1% (invisible). Deriving both
 * from the span keeps the tree looking like the same tree at every size. */
const TRUNK_FRACTION = 0.026;

/* Blossom size scales inversely with how many there are.
 *
 * A fixed fraction cannot serve both ends: sized for a full canopy, a
 * five-task day renders specks you can't identify; sized for five, a busy day
 * becomes overlapping blobs. Each blossom is now a task you're meant to pick
 * out, so few tasks means large flowers. */
function blossomFractionFor(count) {
  return Math.min(0.055, Math.max(0.018, 0.085 / Math.sqrt(Math.max(count, 1))));
}
const BASE_TRUNK_WIDTH = 32;
const BASE_BLOSSOM_RADIUS = 6;

/* Fit the frame to what's on screen.
 *
 * A fixed viewBox sized for the finished tree leaves a sapling as a speck in
 * the bottom of the panel. Measuring the visible geometry instead means every
 * stage fills the frame and the tree grows toward the viewer.
 *
 * Done in two passes because blossom size depends on the span, and the span
 * depends on where the blossoms land: branches first to get the scale, then
 * everything to get the final box. */
function fitViewBox(branches, blossoms) {
  const bounds = () => ({ minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity });
  const include = (b, x, y, pad) => {
    b.minX = Math.min(b.minX, x - pad);
    b.maxX = Math.max(b.maxX, x + pad);
    b.minY = Math.min(b.minY, y - pad);
    b.maxY = Math.max(b.maxY, y + pad);
  };

  const branchBounds = bounds();
  branches.forEach((branch) => {
    branch.points.forEach(([x, y]) => include(branchBounds, x, y, 0));
  });
  include(branchBounds, 0, 0, 0);

  const span = Math.max(
    branchBounds.maxX - branchBounds.minX,
    branchBounds.maxY - branchBounds.minY,
    1
  );

  const strokeScale = (span * TRUNK_FRACTION) / BASE_TRUNK_WIDTH;
  const blossomScale = (span * blossomFractionFor(blossoms.length)) / BASE_BLOSSOM_RADIUS;

  const full = bounds();
  branches.forEach((branch) => {
    branch.points.forEach(([x, y]) => include(full, x, y, branch.width * strokeScale * 0.5));
  });
  blossoms.forEach((blossom) => include(full, blossom.x, blossom.y, blossom.r * blossomScale));
  include(full, 0, 0, span * TRUNK_FRACTION);

  const width = full.maxX - full.minX;
  const height = full.maxY - full.minY;
  const pad = Math.max(width, height) * 0.07;

  return {
    box: [full.minX - pad, full.minY - pad, width + pad * 2, height + pad * 2],
    groundRadius: Math.max(width * 0.4, span * 0.08),
    strokeScale,
    blossomScale,
  };
}

function element(name, attributes) {
  const node = document.createElementNS(SVG_NS, name);
  for (const key in attributes) node.setAttribute(key, attributes[key]);
  return node;
}

/* A blossom, in one of two states.
 *
 * Done work is in full bloom; work still outstanding is a closed bud on the
 * same branch. That's the whole metaphor: the tree's *size* is how much you're
 * carrying, and how much of it is flowering is how much you've actually done. */
function blossomNode(blossom, scale, task) {
  const done = !task || task.done;
  // A bud is smaller and tighter than an open flower.
  const radius = blossom.r * scale * (done ? 1 : 0.82);

  const outer = element("g", {
    transform:
      "translate(" + blossom.x.toFixed(2) + " " + blossom.y.toFixed(2) +
      ") rotate(" + blossom.rot.toFixed(1) + ")",
  });
  if (task) {
    outer.setAttribute("class", "cbt-task");
    outer.setAttribute("data-task-id", task.id);
    const label = element("title", {});
    label.textContent = task.title + (task.done ? " — done" : "");
    outer.appendChild(label);
  }

  // The inner group owns the bloom animation so its transform never clobbers
  // the placement transform on the outer one.
  const inner = element("g", { class: "cbt-blossom" + (done ? "" : " cbt-bud") });
  inner.style.animationDelay = blossom.delay.toFixed(2) + "s";

  [0, 72, 144, 216, 288].forEach((angle) => {
    inner.appendChild(
      element("ellipse", {
        cx: 0,
        // A bud's petals are drawn tighter around the centre than an open
        // flower's, so it reads as closed rather than merely small.
        cy: (-radius * (done ? 0.62 : 0.34)).toFixed(2),
        rx: (radius * (done ? 0.44 : 0.5)).toFixed(2),
        ry: (radius * (done ? 0.66 : 0.55)).toFixed(2),
        transform: "rotate(" + angle + ")",
        class: "cbt-petal cbt-petal-" + blossom.tone + (done ? "" : " cbt-petal-closed"),
      })
    );
  });
  if (done) {
    inner.appendChild(element("circle", { r: (radius * 0.2).toFixed(2), class: "cbt-stamen" }));
  }

  outer.appendChild(inner);
  return outer;
}

let cachedTree = null;

/* How far out the tree has to reach to carry this many tasks.
 *
 * Each level roughly doubles the number of tips, so the canopy widens as work
 * accumulates: a couple of tasks is a sapling, a full day's list is a spreading
 * tree. Floor of 2 so an empty day still shows a trunk rather than nothing. */
function depthForTaskCount(branches, taskCount) {
  for (let depth = 2; depth <= MAX_DEPTH; depth++) {
    const tips = branches.filter((branch) => branch.depth === depth - 1).length;
    if (tips >= taskCount) return depth;
  }
  return MAX_DEPTH;
}

/**
 * Render the tree into `container`.
 *
 * `tasks` is [{ id, title, kind, done }] -- one blossom each. The tree's
 * structure is derived from how many there are, so adding a task genuinely
 * branches the tree rather than just decorating it.
 */
export function renderCherryBlossom(container, tasks, options) {
  const settings = options || {};
  if (!cachedTree) cachedTree = buildTree(settings.seed || 20260912);

  const list = Array.isArray(tasks) ? tasks : [];
  const maxDepth = depthForTaskCount(cachedTree, Math.max(list.length, 1));

  const visible = cachedTree.filter((branch) => branch.depth < maxDepth);
  const tips = visible.filter((branch) => branch.depth === maxDepth - 1);

  // Spread tasks evenly across the available tips instead of filling from one
  // side, so three tasks sit across the canopy rather than bunched left.
  const placements = list.map((task, index) => {
    const tip = tips[Math.floor((index * tips.length) / Math.max(list.length, 1))] || tips[0];
    return { task, blossom: tip && tip.blossoms[index % Math.max(tip.blossoms.length, 1)] };
  }).filter((placement) => placement.blossom);

  const shownBlossoms = placements.map((placement) => placement.blossom);
  const { box, groundRadius, strokeScale, blossomScale } = fitViewBox(visible, shownBlossoms);

  const svg = element("svg", {
    class: "cbt-canvas",
    viewBox: box.map((n) => n.toFixed(1)).join(" "),
    preserveAspectRatio: "xMidYMax meet",
    "aria-hidden": "true",
  });

  const defs = element("defs", {});
  defs.innerHTML =
    '<linearGradient id="cbt-bark" x1="0" y1="1" x2="0.35" y2="0">' +
    '<stop offset="0%" stop-color="var(--bark-root)"/>' +
    '<stop offset="45%" stop-color="var(--bark-mid)"/>' +
    '<stop offset="100%" stop-color="var(--bark-tip)"/></linearGradient>' +
    '<radialGradient id="cbt-canopy" cx="50%" cy="42%" r="52%">' +
    '<stop offset="0%" stop-color="var(--canopy-glow)" stop-opacity="0.75"/>' +
    '<stop offset="100%" stop-color="var(--canopy-glow)" stop-opacity="0"/></radialGradient>' +
    '<filter id="cbt-soft" x="-40%" y="-40%" width="180%" height="180%">' +
    '<feGaussianBlur stdDeviation="7"/></filter>';
  svg.appendChild(defs);

  svg.appendChild(
    element("ellipse", {
      cx: 0, cy: 4,
      rx: groundRadius.toFixed(1), ry: (groundRadius * 0.1).toFixed(1),
      class: "cbt-ground", filter: "url(#cbt-soft)",
    })
  );

  const sway = element("g", { class: "cbt-sway" });
  // No fixed scale: the fitted viewBox already sizes the tree to the frame.
  const scaled = element("g", {});

  // The glow marks a canopy actually in bloom, so it follows completed work
  // rather than simply appearing whenever the tree is large.
  const doneCount = placements.filter((placement) => placement.task.done).length;
  if (shownBlossoms.length && doneCount >= Math.ceil(placements.length / 2)) {
    const cx = shownBlossoms.reduce((sum, b) => sum + b.x, 0) / shownBlossoms.length;
    const cy = shownBlossoms.reduce((sum, b) => sum + b.y, 0) / shownBlossoms.length;
    scaled.appendChild(
      element("ellipse", {
        cx: cx.toFixed(1), cy: cy.toFixed(1),
        rx: (box[2] * 0.38).toFixed(1), ry: (box[3] * 0.3).toFixed(1),
        fill: "url(#cbt-canopy)", class: "cbt-canopy-glow",
      })
    );
  }

  visible.forEach((branch) => {
    const path = element("path", {
      class: "cbt-branch",
      d: branch.d,
      pathLength: 1,
      stroke: "url(#cbt-bark)",
      "stroke-width": (branch.width * strokeScale).toFixed(2),
      "stroke-linecap": "round",
      fill: "none",
    });
    path.style.animationDelay = branch.delay.toFixed(2) + "s";
    path.style.animationDuration = branch.duration.toFixed(2) + "s";
    scaled.appendChild(path);
  });

  // One blossom per task, at the tips, so the canopy is literally the day's
  // work rather than decoration sitting behind it.
  placements.forEach((placement) => {
    scaled.appendChild(blossomNode(placement.blossom, blossomScale, placement.task));
  });

  sway.appendChild(scaled);
  svg.appendChild(sway);

  container.innerHTML = "";
  container.appendChild(svg);
  // Petals only fall from a tree that has bloomed.
  container.appendChild(
    buildDrifters(settings.seed || 20260912, doneCount, Math.max(placements.length, 1))
  );

  return { depth: maxDepth, tasks: placements.length, done: doneCount };
}

function buildDrifters(seed, stage, stageCount) {
  const rand = makeRandom(seed + 991);
  const fall = document.createElement("div");
  fall.className = "cbt-fall";
  fall.setAttribute("aria-hidden", "true");

  // A bare tree shouldn't be shedding petals.
  const ratio = stageCount > 1 ? stage / (stageCount - 1) : 1;
  const count = Math.round(DRIFTER_COUNT * ratio);

  for (let i = 0; i < count; i++) {
    const petal = document.createElement("span");
    petal.className = "cbt-drifter cbt-petal-" + Math.floor(rand() * 3) + "-bg";
    petal.style.left = (rand() * 100).toFixed(1) + "%";
    const size = 5 + rand() * 5;
    petal.style.width = size.toFixed(1) + "px";
    petal.style.height = (size * 0.82).toFixed(1) + "px";
    petal.style.animationDelay = (rand() * 18).toFixed(2) + "s";
    petal.style.animationDuration = (13 + rand() * 13).toFixed(2) + "s";
    petal.style.setProperty("--cbt-drift", ((rand() - 0.5) * 220).toFixed(0) + "px");
    petal.style.setProperty("--cbt-spin", (240 + rand() * 500).toFixed(0) + "deg");
    fall.appendChild(petal);
  }
  return fall;
}
