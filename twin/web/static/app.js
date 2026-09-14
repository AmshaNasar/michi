/*
 * Michi front end.
 *
 * Deliberately vanilla: the page has one screen, a handful of actions, and no
 * routing, so a framework would add a build step and a second runtime for no
 * benefit on a local single-user app.
 *
 * Every mutating call returns the full new state, so the UI never has to
 * reconcile an optimistic guess against what the twin actually did.
 */

import { renderCherryBlossom } from "/static/tree.js";

const KIND_ICON = {
  email: "#i-mail",
  calendar: "#i-calendar",
  project: "#i-sprout",
  deadline: "#i-clock",
};

const XP_PER_QUEST = 40;

const el = (id) => document.getElementById(id);
let state = null;
let busy = false;

/* ---------------------------------------------------------------- fetching */

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) {
      /* response wasn't JSON; the status text will do */
    }
    throw new Error(detail);
  }
  return response.json();
}

async function refresh() {
  try {
    render(await api("/api/state"));
  } catch (error) {
    el("notice").textContent = "Couldn't reach the twin: " + error.message;
  }
}

/* --------------------------------------------------------------- rendering */

function render(next) {
  state = next;

  el("greeting").textContent = state.greeting;
  el("today-date").textContent = state.date;
  el("notice").textContent = state.notice;

  el("stage-name").textContent = state.stage_name;
  el("progress-value").textContent = state.progress + "%";
  el("growth-fill").style.width = state.progress + "%";
  el("cleared-label").textContent =
    state.completed_today + " of " + state.total_today + " cleared today";
  el("xp-label").textContent = "+" + state.completed_today * XP_PER_QUEST + " bloom XP";
  el("reward-chip").textContent = state.quests.length * XP_PER_QUEST + " XP";

  el("nav-inbox").textContent = state.open_deadlines;
  el("nav-projects").textContent = state.projects;
  const plural = (count, word) => count + " " + word + (count === 1 ? "" : "s");
  el("streak-value").textContent = plural(state.streak, "day") + " streak";
  el("streak-kept").textContent = plural(state.kept_this_week, "commitment") + " kept";

  // Rank is derived from the streak so the badge means something rather than
  // being decoration.
  const rank = Math.min(5, 1 + Math.floor(state.streak / 3));
  el("rank-label").textContent = "Garden " + "I".repeat(rank).replace("IIII", "IV");

  const avatar = el("avatar");
  avatar.textContent = state.greeting.includes(",")
    ? state.greeting.split(",")[1].trim()[0].toUpperCase()
    : "·";

  renderTree();
  renderStages();
  renderQuests();

  el("footnote").textContent = state.connected.length
    ? "Connected: " + state.connected.join(", ")
    : "No accounts connected yet — run onboarding to add Gmail, Slack, or Notion";
}

let renderedSignature = null;

/* The tree is the task list. Its size comes from how many tasks there are and
 * its bloom from how many are done, so it only needs rebuilding when that
 * actually changes -- otherwise the 60s poll would replay the whole
 * grow-and-bloom animation every minute. */
function taskSignature(tasks) {
  return tasks.map((task) => task.id + (task.done ? ":1" : ":0")).join("|");
}

function renderTree(tasksOverride) {
  const tasks = tasksOverride || state.tasks || [];
  const signature = taskSignature(tasks);
  if (renderedSignature === signature) return;
  renderedSignature = signature;

  const result = renderCherryBlossom(el("tree"), tasks);
  const done = result.done;
  el("tree-caption").textContent =
    "Cherry blossom tree carrying " + result.tasks + " task" +
    (result.tasks === 1 ? "" : "s") + ", " + done + " in bloom";

  wirePetalHover();
}

/* Hovering a petal names its task, and hovering a task lights its petal --
 * without that the mapping is a claim rather than something you can see. */
function wirePetalHover() {
  document.querySelectorAll(".cbt-task").forEach((petal) => {
    const id = petal.getAttribute("data-task-id");
    petal.addEventListener("mouseenter", () => highlightTask(id, true));
    petal.addEventListener("mouseleave", () => highlightTask(id, false));
  });
}

function highlightTask(id, on) {
  const row = document.querySelector('[data-quest-id="' + CSS.escape(id) + '"]');
  if (row) row.classList.toggle("quest-lit", on);
  const petal = document.querySelector('.cbt-task[data-task-id="' + CSS.escape(id) + '"]');
  if (petal) petal.classList.toggle("cbt-task-lit", on);
}

function renderStages() {
  const row = el("stage-row");
  row.innerHTML = "";
  state.stage_names.forEach((name, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "stage-button" + (index === state.stage ? " stage-button-active" : "");
    button.title = name;
    button.setAttribute("aria-label", name + ", stage " + (index + 1));
    button.innerHTML =
      '<span class="stage-dot"><svg class="ico stage-flower"><use href="#i-flower"/></svg></span>' +
      '<span class="stage-number">' + (index + 1) + "</span>";
    // Preview only -- looking ahead shouldn't rewrite what you've actually done.
    button.addEventListener("click", () => previewStage(index));
    row.appendChild(button);
  });
}

/* Preview shows the same tasks with more of them done -- "here is your day if
 * you finish these" -- rather than an abstract stage. */
function previewStage(index) {
  const tasks = (state.tasks || []).map((task, position) => ({
    ...task,
    done: position < Math.round(((index + 1) / state.stage_names.length) * state.tasks.length),
  }));
  renderTree(tasks);
  el("stage-name").textContent = state.stage_names[index];
  document.querySelectorAll(".stage-button").forEach((button, position) => {
    button.classList.toggle("stage-button-active", position === index);
  });
}

function renderQuests() {
  const list = el("quest-list");
  list.innerHTML = "";

  if (!state.quests.length) {
    list.innerHTML =
      '<p class="empty-state">Nothing outstanding. The twin will speak up when that changes.</p>';
    return;
  }

  state.quests.forEach((quest) => {
    const item = document.createElement("article");
    item.className = "quest";
    item.setAttribute("data-quest-id", quest.id);
    item.addEventListener("mouseenter", () => highlightTask(quest.id, true));
    item.addEventListener("mouseleave", () => highlightTask(quest.id, false));

    const check = document.createElement("button");
    check.className = "check-button";
    check.title = "Mark done";
    check.setAttribute("aria-label", "Complete " + quest.title);
    check.innerHTML = '<svg class="ico ico-sm"><use href="#i-circle"/></svg>';
    check.addEventListener("click", () => completeQuest(quest, item));

    const icon = document.createElement("span");
    icon.className = "quest-icon";
    icon.innerHTML =
      '<svg class="ico ico-sm"><use href="' + (KIND_ICON[quest.kind] || "#i-clock") + '"/></svg>';

    const body = document.createElement("div");
    body.className = "quest-body";
    const title = document.createElement("h3");
    title.className = "quest-title";
    title.textContent = quest.title;
    const detail = document.createElement("p");
    detail.className = "quest-detail";
    detail.textContent = quest.detail;
    body.append(title, detail);

    const time = document.createElement("span");
    time.className = "quest-time";
    time.textContent = quest.time;

    item.append(check, icon, body, time);

    if (quest.deferrable) {
      const defer = document.createElement("button");
      defer.className = "text-button";
      defer.textContent = "Defer";
      defer.addEventListener("click", () => deferQuest(quest));
      item.appendChild(defer);
    }

    const review = document.createElement("button");
    review.className = "icon-button-subtle";
    review.setAttribute("aria-label", "Ask about " + quest.title);
    review.innerHTML = '<svg class="ico"><use href="#i-chevron"/></svg>';
    review.addEventListener("click", () => {
      el("prompt").value = "Tell me more about: " + quest.title;
      el("prompt").focus();
    });
    item.appendChild(review);

    list.appendChild(item);
  });
}

/* ----------------------------------------------------------------- actions */

async function completeQuest(quest, node) {
  // Show the tick immediately; the real state arrives a moment later.
  node.classList.add("quest-done");
  node.querySelector(".check-button").classList.add("check-button-done");
  node.querySelector(".check-button").innerHTML =
    '<svg class="ico ico-sm"><use href="#i-check"/></svg>';

  try {
    render(await api("/api/quests/" + quest.id + "/complete", { method: "POST" }));
    dropPetals();
  } catch (error) {
    node.classList.remove("quest-done");
    el("notice").textContent = "Couldn't complete that: " + error.message;
  }
}

async function deferQuest(quest) {
  try {
    render(await api("/api/quests/" + quest.id + "/defer", { method: "POST" }));
  } catch (error) {
    el("notice").textContent = "Couldn't defer that: " + error.message;
  }
}

function dropPetals() {
  const stage = document.querySelector(".tree-stage");
  ["petal-one", "petal-two"].forEach((position) => {
    const petal = document.createElement("div");
    petal.className = "petal " + position;
    petal.setAttribute("aria-hidden", "true");
    stage.appendChild(petal);
    setTimeout(() => petal.remove(), 2200);
  });
}

/* -------------------------------------------------------------------- chat */

function addBubble(text, who, pending) {
  const bubble = document.createElement("div");
  bubble.className = "bubble bubble-" + who + (pending ? " bubble-pending" : "");
  bubble.textContent = text;
  const transcript = el("transcript");
  transcript.appendChild(bubble);
  transcript.scrollTop = transcript.scrollHeight;
  return bubble;
}

async function sendPrompt(event) {
  event.preventDefault();
  const input = el("prompt");
  const message = input.value.trim();
  if (!message || busy) return;

  busy = true;
  el("send").disabled = true;
  input.value = "";
  addBubble(message, "user");
  const pending = addBubble("thinking…", "twin", true);

  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    pending.remove();
    addBubble(result.reply, "twin");
    render(result.state);
  } catch (error) {
    pending.remove();
    addBubble("Couldn't reach the agent: " + error.message, "twin");
  } finally {
    busy = false;
    el("send").disabled = false;
    input.focus();
  }
}

/* -------------------------------------------------------------------- init */

el("chat-form").addEventListener("submit", sendPrompt);
el("refresh").addEventListener("click", refresh);

el("toggle-add").addEventListener("click", () => {
  const form = el("add-form");
  form.hidden = !form.hidden;
  if (!form.hidden) el("new-project").focus();
});

el("add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = el("new-project");
  const name = input.value.trim();
  if (!name) return;
  try {
    render(await api("/api/projects", { method: "POST", body: JSON.stringify({ name }) }));
    input.value = "";
    el("add-form").hidden = true;
  } catch (error) {
    el("notice").textContent = "Couldn't track that: " + error.message;
  }
});

el("mic").addEventListener("click", () => {
  addBubble(
    "Voice runs in the terminal for now — it needs microphone access the browser would " +
      "have to ask for separately. Run: twin.cli voice",
    "twin"
  );
});

refresh();
// The scheduler queues nudges in the background, so pick them up periodically.
setInterval(refresh, 60000);
