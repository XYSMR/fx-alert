const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.handlers = {};
  }

  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  focus() {}
}

function find(node, predicate) {
  if (!(node instanceof Element)) return undefined;
  if (predicate(node)) return node;
  for (const child of node.children) {
    const found = find(child, predicate);
    if (found) return found;
  }
  return undefined;
}

const withId = (node, id) => find(node, (element) => element.attributes.id === id);
const byLabel = (node, label) => find(node, (element) => element.attributes["aria-label"] === label);
const submit = (form) => form.handlers.submit({ preventDefault() {} });

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function setup({ trip = true, quick = true } = {}) {
  const nodes = new Map();
  const calls = [];
  const behavior = { confirm: true, insert: {} };
  const document = {
    createElement: (tag) => new Element(tag),
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Element("div"));
      return nodes.get(id);
    },
  };
  const client = {
    from(table) {
      return {
        select() { return this; },
        order() { return this; },
        limit() { return this; },
        then(resolve) { resolve({ data: [], error: null }); },
        insert(payload) {
          calls.push({ table, payload: { ...payload } });
          return behavior.insert[table]?.() ?? Promise.resolve({ error: null });
        },
      };
    },
  };
  const window = {
    FxFlags: {
      clear() {},
      isOn: (flag) => flag === "trip_plan" ? trip : quick,
      async load() {},
    },
  };
  const sandbox = vm.createContext({ window, document, clientFixture: client, confirm: () => behavior.confirm });
  for (const file of ["trips.js", "account.js"]) {
    vm.runInContext(readFileSync(join(__dirname, "..", file), "utf8"), sandbox);
  }
  const refresh = () => vm.runInContext("refresh()", sandbox);
  vm.runInContext([
    "client = clientFixture;",
    'state.session = { user: { id: "fixture-user" } };',
    'publicList = [{ code: "JPY", name: "JPY", rate: "0.2119", popular_rank: 1 }];',
    "loadMine = async () => true;",
    "renderAccount = () => {};",
    "renderNotifications = () => {};",
    "renderEmailSwitch = () => null;",
    "renderNotifyWindow = () => null;",
    "deleteSection = () => null;",
  ].join("\n"), sandbox);
  await refresh();
  return {
    calls,
    behavior,
    alert: () => withId(nodes.get("my-rules-body"), "add-form"),
    trips: () => withId(nodes.get("my-trips-body"), "trip-add-form"),
    message: (key) => nodes.get(key)?.textContent,
    events: () => calls.filter(({ table }) => table === "experiment_events").map(({ payload }) => payload),
    signOut: async () => {
      vm.runInContext("state.session = null", sandbox);
      await refresh();
    },
  };
}

test("Quick Target ON records one selection per form; it saves only after the explicit Add", async () => {
  const ui = await setup();
  const form = ui.alert();
  assert.deepEqual(ui.calls, []);
  const first = byLabel(form, "填入 0.2098");
  const second = byLabel(form, "填入 0.2077");
  first.handlers.click();
  second.handlers.click();
  assert.equal(withId(form, "add-target").value, "0.2077");
  assert.deepEqual(ui.events(), [{ event: "quick_target_clicked", flag: "quick_target" }]);
  const write = deferred();
  ui.behavior.insert.alert_rules = () => write.promise;
  const saved = submit(form);
  await submit(form);
  assert.equal(ui.calls.filter(({ table }) => table === "alert_rules").length, 1);
  assert.equal(ui.events().length, 1);
  write.resolve({ error: null });
  await saved;
  assert.deepEqual(ui.events(), [
    { event: "quick_target_clicked", flag: "quick_target" },
    { event: "alert_saved", flag: "quick_target" },
  ]);
});

test("Quick Target OFF ignores shortcut activation and records only successful manual saves", async () => {
  const ui = await setup({ trip: false, quick: false });
  assert.equal(ui.trips(), undefined);
  assert.deepEqual(ui.calls, []);
  const form = ui.alert();
  const target = withId(form, "add-target");
  byLabel(form, "填入 0.2098").handlers.click();
  assert.equal(target.value, undefined);
  target.value = "invalid";
  await submit(form);
  target.value = "0.0200";
  ui.behavior.confirm = false;
  await submit(form);
  assert.deepEqual(ui.calls, []);

  target.value = "0.2098";
  ui.behavior.insert.alert_rules = async () => ({ error: { code: "400" } });
  await submit(form);
  assert.deepEqual(ui.events(), []);
  ui.behavior.insert.alert_rules = async () => ({ error: null });
  const telemetry = deferred();
  ui.behavior.insert.experiment_events = () => telemetry.promise;
  await submit(form);
  assert.match(ui.message("my-rules-status"), /已新增/);
  assert.deepEqual(ui.events(), [{ event: "alert_saved", flag: "quick_target" }]);
  telemetry.resolve({ error: { code: "42501" } });
  assert.equal(ui.events().length, 1);
});

test("a shortcut cannot change the target or record a click during a manual save", async () => {
  const ui = await setup();
  const form = ui.alert();
  const target = withId(form, "add-target");
  target.value = "0.2100";
  const write = deferred();
  ui.behavior.insert.alert_rules = () => write.promise;
  const saved = submit(form);
  byLabel(form, "填入 0.2098").handlers.click();
  assert.equal(target.value, "0.2100");
  assert.deepEqual(ui.calls, [{ table: "alert_rules", payload: { currency: "JPY", target: "0.21" } }]);
  write.resolve({ error: null });
  await saved;
  assert.deepEqual(ui.events(), [{ event: "alert_saved", flag: "quick_target" }]);
});

test("Trip Plan records a successful creation once and preserves success on a telemetry rejection", async () => {
  const ui = await setup();
  const form = ui.trips();
  const departure = withId(form, "trip-departure");
  const amount = withId(form, "trip-amount");
  withId(form, "trip-name").value = "";
  withId(form, "trip-currency").value = "JPY";
  departure.value = "";
  amount.value = "500";
  await submit(form);
  assert.deepEqual(ui.calls, []);

  departure.value = departure.attributes.min;
  ui.behavior.insert.trips = async () => ({ error: { code: "42501" } });
  await submit(form);
  assert.deepEqual(ui.events(), []);
  const write = deferred();
  ui.behavior.insert.trips = () => write.promise;
  ui.behavior.insert.experiment_events = async () => { throw new Error("fixture network failure"); };
  const saved = submit(form);
  await submit(form);
  assert.equal(ui.calls.filter(({ table }) => table === "trips").length, 2);
  assert.deepEqual(ui.events(), []);
  write.resolve({ error: null });
  await saved;
  assert.deepEqual(ui.events(), [{ event: "trip_created", flag: "trip_plan" }]);
  assert.match(ui.message("my-trips-status"), /已建立/);
});

test("A completed write from a signed-out form cannot enqueue telemetry", async () => {
  const ui = await setup();
  const form = ui.alert();
  withId(form, "add-target").value = "0.2098";
  const write = deferred();
  ui.behavior.insert.alert_rules = () => write.promise;
  const saved = submit(form);
  await ui.signOut();
  write.resolve({ error: null });
  await saved;
  assert.deepEqual(ui.events(), []);
});
