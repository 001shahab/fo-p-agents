/* ==========================================================================
   Agent test harness - interface
   Author: Prof. Shahab Anbarjafari

   A way in, a welcome, and then three screens: choose an agent, build data for
   it, watch it run. There is one thing to do on each of them, and the thing to
   do is always the only orange button on screen.

   React is here as two vendored files rather than a build step, so that
   `python app.py` is the whole of the set-up. Views are written with
   createElement through the small `ui` helper below instead of JSX, for the
   same reason: JSX would need a compiler, and a compiler would need a
   toolchain nobody asked for.
   ========================================================================== */

(function () {
    "use strict";

    var e = React.createElement;
    var useState = React.useState;
    var useEffect = React.useEffect;
    var useRef = React.useRef;
    var useCallback = React.useCallback;

    /* ui.div(props, ...children) reads closely enough to markup to be
       maintainable, without asking anyone to install anything. */
    var ui = new Proxy({}, {
        get: function (_, tag) {
            return function (props) {
                var children = Array.prototype.slice.call(arguments, 1);
                return e.apply(null, [tag, props === undefined ? null : props].concat(children));
            };
        }
    });

    // ----------------------------------------------------------------------
    // Talking to the server
    // ----------------------------------------------------------------------

    /* Handed out by /api/unlock and required by everything after it. Held here
       and nowhere else: not in storage, not in a cookie, so closing the tab
       ends the session and opening the page again asks for the phrase. */
    var session = "";

    function api(path, options) {
        var settings = options || {};
        settings.headers = Object.assign({}, settings.headers,
            session ? { "X-Session": session } : {});
        return fetch(path, settings).then(function (response) {
            return response.json().catch(function () {
                return { error: "The server sent something that was not JSON." };
            }).then(function (payload) {
                if (!response.ok) { throw new Error(payload.error || "Request failed."); }
                return payload;
            });
        });
    }

    function postJSON(path, body) {
        return api(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {})
        });
    }

    /* An EventSource cannot send a header and neither can a download link, so
       for those two the token travels in the query string instead. */
    function withToken(path) {
        return path + (path.indexOf("?") >= 0 ? "&" : "?") +
               "token=" + encodeURIComponent(session);
    }

    // ----------------------------------------------------------------------
    // Small shared pieces
    // ----------------------------------------------------------------------

    function count(value) {
        return (value || 0).toLocaleString("en-GB");
    }

    function bytes(value) {
        if (value < 1024) { return value + " B"; }
        if (value < 1024 * 1024) { return (value / 1024).toFixed(0) + " KB"; }
        return (value / 1024 / 1024).toFixed(1) + " MB";
    }

    function elapsedWords(seconds) {
        if (seconds < 60) { return seconds + (seconds === 1 ? " second" : " seconds"); }
        var minutes = Math.floor(seconds / 60);
        var rest = seconds % 60;
        return minutes + "m " + (rest < 10 ? "0" : "") + rest + "s";
    }

    function Masthead(props) {
        var steps = ["Choose", "Data", "Test"];
        var order = { choose: 0, data: 1, test: 2 };
        var here = order[props.step];

        return ui.header({ className: "masthead" },
            ui.img({ className: "masthead__logo", src: "img/logo.png", alt: "PwC" }),
            ui.ol({ className: "stepper" }, steps.map(function (label, index) {
                var state = index === here ? "current" : (index < here ? "done" : "todo");
                return ui.li({ key: label, className: "stepper__step stepper__step--" + state },
                    index > 0 ? ui.span({ className: "stepper__rule" }) : null,
                    ui.span({ className: "stepper__dot" }),
                    ui.span(null, label));
            })));
    }

    function Working(props) {
        return ui.div({ className: "working" },
            ui.div({ className: "spinner" }),
            ui.p({ className: "working__label" }, props.label));
    }

    function Problem(props) {
        if (!props.message) { return null; }
        return ui.div({ className: "problem" }, props.message);
    }

    /* One table, shown the way a spreadsheet would show it: headers pinned,
       every value visible, nothing reformatted on the way through. */
    function Grid(props) {
        var columns = props.columns || [];
        var rows = props.rows || [];
        if (!columns.length) {
            return ui.p({ className: "file-note" }, "This file is empty.");
        }
        return ui.div({ className: "table-frame" },
            ui.table({ className: "grid" },
                ui.thead(null, ui.tr(null, columns.map(function (name) {
                    return ui.th({ key: name }, name);
                }))),
                ui.tbody(null, rows.map(function (row, index) {
                    return ui.tr({ key: index }, columns.map(function (name, column) {
                        var value = row[column];
                        return ui.td({ key: name, title: value }, value === "" ? "—" : value);
                    }));
                }))));
    }

    // ----------------------------------------------------------------------
    // The way in
    // ----------------------------------------------------------------------

    function GateScreen(props) {
        var entered = useState("");
        var phrase = entered[0];
        var setPhrase = entered[1];
        var refusedState = useState("");
        var refused = refusedState[0];
        var setRefused = refusedState[1];
        var checkingState = useState(false);
        var checking = checkingState[0];
        var setChecking = checkingState[1];
        /* Counts refusals. Used as the form's key so that the shake replays on
           every wrong answer rather than only the first. */
        var refusalState = useState(0);
        var refusals = refusalState[0];
        var setRefusals = refusalState[1];
        var field = useRef(null);

        useEffect(function () { if (field.current) { field.current.focus(); } }, [refusals]);

        function submit(event) {
            event.preventDefault();
            if (!phrase || checking) { return; }
            setChecking(true);
            setRefused("");
            postJSON("/api/unlock", { password: phrase })
                .then(function (payload) { props.onOpen(payload.token); })
                .catch(function (problem) {
                    setChecking(false);
                    setPhrase("");
                    setRefused(problem.message);
                    setRefusals(function (value) { return value + 1; });
                });
        }

        return ui.section({ className: "gate" },
            ui.img({ className: "gate__logo", src: "img/logo.png", alt: "PwC" }),
            ui.h1({ className: "gate__title" }, "Agents Test AI platform"),
            ui.p({ className: "gate__note" },
                "This machine runs the procurement agents against invented data. " +
                "Enter the passphrase to continue."),
            ui.form({
                className: "gate__form" + (refused ? " gate__form--wrong" : ""),
                onSubmit: submit,
                key: refusals
            },
                ui.input({
                    ref: field,
                    className: "gate__input",
                    type: "password",
                    value: phrase,
                    placeholder: "Passphrase",
                    autoComplete: "off",
                    spellCheck: false,
                    "aria-label": "Passphrase",
                    onChange: function (event) { setPhrase(event.target.value); }
                }),
                ui.button({
                    className: "btn btn--primary",
                    type: "submit",
                    disabled: !phrase || checking
                }, checking ? "Checking" : "Enter")),
            ui.p({ className: "gate__refusal" }, refused));
    }

    /* Three and a half seconds, and it can be cut short by touching anything.
       Long enough to feel like an arrival, short enough that nobody who runs
       this twice a day comes to resent it. */
    function WelcomeScreen(props) {
        var hintState = useState(false);
        var hint = hintState[0];
        var setHint = hintState[1];

        useEffect(function () {
            var show = window.setTimeout(function () { setHint(true); }, 2100);
            var leave = window.setTimeout(props.onDone, 3600);
            function skip() { props.onDone(); }
            window.addEventListener("keydown", skip);
            return function () {
                window.clearTimeout(show);
                window.clearTimeout(leave);
                window.removeEventListener("keydown", skip);
            };
        }, [props.onDone]);

        return ui.section({ className: "welcome", onClick: props.onDone },
            ui.div({ className: "welcome__stage" },
                ui.span({ className: "welcome__glow" }),
                ui.img({ className: "welcome__logo", src: "img/logo.png", alt: "PwC" })),
            ui.div({ className: "welcome__rule" }),
            ui.h1({ className: "welcome__line" }, "Welcome to Agents Test AI platform"),
            hint ? ui.p({ className: "welcome__hint" }, "Click to continue") : null);
    }

    // ----------------------------------------------------------------------
    // Screen one: choosing an agent
    // ----------------------------------------------------------------------

    function ChooseScreen(props) {
        return ui.section({ className: "screen" },
            ui.div({ className: "screen__head" },
                ui.p({ className: "eyebrow" }, "Agent test harness"),
                ui.h1({ className: "title" }, "Which agent should we test?"),
                ui.p({ className: "subtitle" },
                    "Each one is given data invented for the purpose, built to contain " +
                    "exactly what that agent is supposed to find.")),

            ui.div({ className: "agents" }, props.agents.map(function (agent) {
                var chosen = props.chosen === agent.key;
                return ui.button({
                    key: agent.key,
                    className: "agent",
                    type: "button",
                    "aria-pressed": chosen,
                    onClick: function () { props.onChoose(agent.key); },
                    onDoubleClick: props.onNext
                },
                    ui.span({ className: "agent__number" }, agent.number),
                    ui.span(null,
                        ui.h2({ className: "agent__name" }, agent.name),
                        ui.p({ className: "agent__tagline" }, agent.tagline),
                        ui.p({ className: "agent__script" }, agent.script)));
            })),

            ui.div({ className: "actions" },
                ui.button({
                    className: "btn btn--primary",
                    type: "button",
                    disabled: !props.chosen,
                    onClick: props.onNext
                }, "Next")),

            ui.p({ className: "hint" },
                props.chosen
                    ? [ui.span({ key: "a" }, "Press "), e("kbd", { key: "b" }, "Enter"),
                       ui.span({ key: "c" }, " to continue")]
                    : "Choose an agent to continue"));
    }

    // ----------------------------------------------------------------------
    // Screen two: the data
    // ----------------------------------------------------------------------

    /* What the agent is for, said once, for a reader who will never run it. */
    function Brief(props) {
        var brief = props.brief;
        if (!brief) { return null; }

        function fact(label, value) {
            return [
                ui.span({ key: label + "-k", className: "brief__label" }, label),
                ui.p({ key: label + "-v", className: "brief__value" }, value)
            ];
        }

        return ui.div({ className: "brief" },
            ui.span({ className: "brief__mark", "aria-hidden": "true" },
                ui.i({ className: "brief__lozenge" })),
            ui.p({ className: "brief__purpose" }, brief.purpose),
            ui.div({ className: "brief__rule" }),
            ui.div({ className: "brief__facts" },
                fact("Given", brief.given),
                fact("Returns", brief.returns),
                fact("Worth", brief.worth)));
    }

    function ModelSwitch(props) {
        var model = props.model || {};
        var note;
        if (!model.configured) {
            note = "No key found in .env, so the harness will write the data and read " +
                   "the log using its own rules. Everything still runs.";
        } else if (props.on) {
            note = "Using " + model.name + " through " + model.label + " to widen the " +
                   "vocabulary of the test data, describe each file and read the agent's " +
                   "log back in plain English.";
        } else {
            note = model.name + " is configured through " + model.label +
                   ". Leave this off to run entirely on this machine.";
        }

        return ui.div({ className: "model" },
            ui.div({ className: "model__text" },
                ui.p({ className: "model__title" }, "Language model"),
                ui.p({ className: "model__note" }, note)),
            ui.button({
                className: "switch",
                type: "button",
                role: "switch",
                "aria-checked": props.on,
                "aria-label": "Use the language model",
                disabled: !model.configured || props.locked,
                onClick: function () { props.onChange(!props.on); }
            }));
    }

    function DataScreen(props) {
        var dataset = props.dataset;
        var files = dataset ? dataset.files : [];
        var chosenFile = useState(0);
        var active = chosenFile[0];
        var setActive = chosenFile[1];
        var extra = useState(null);
        var moreRows = extra[0];
        var setMoreRows = extra[1];

        useEffect(function () { setActive(0); setMoreRows(null); }, [dataset]);

        var file = files[active];
        var preview = file ? (moreRows && moreRows.file === file.name ? moreRows : file.preview) : null;

        var showMore = useCallback(function () {
            if (!file) { return; }
            api("/api/preview?agent=" + encodeURIComponent(props.agent.key) +
                "&file=" + encodeURIComponent(file.name) + "&limit=60")
                .then(setMoreRows)
                .catch(function () { /* the preview already on screen stays */ });
        }, [file, props.agent]);

        return ui.section({ className: "screen" },
            ui.div({ className: "screen__head" },
                ui.p({ className: "eyebrow" }, "Agent " + props.agent.number),
                ui.h1({ className: "title" }, props.agent.name),
                e(Brief, { brief: props.agent.brief })),

            /* The switch sits in the same place whether or not the data has
               been built, because it governs both: the model widens the
               vocabulary of the data and then reads the run back. */
            e(ModelSwitch, {
                model: props.model,
                on: props.useModel,
                onChange: props.onModel,
                locked: props.building
            }),

            !dataset && !props.building
                ? ui.div(null,
                    ui.div({ className: "card card--quiet" },
                        ui.h2({ className: "card__heading" }, "Nothing real is used"),
                        ui.p({ className: "card__body" },
                            "The harness invents an energy company's procurement estate — sites " +
                            "in Finland, Sweden, Poland and Norway, the suppliers who serve them, " +
                            "and the purchase lines they raise in four languages — and then plants " +
                            "in it the things this agent is meant to find. Because the answer is " +
                            "decided before the data is written, the agent can be marked against it.")),
                    ui.div({ className: "actions" },
                        ui.button({ className: "btn btn--ghost", type: "button", onClick: props.onBack }, "Back"),
                        ui.button({ className: "btn btn--primary", type: "button", onClick: props.onBuild },
                            "Build the data")),
                    ui.p({ className: "hint" }, "Takes a moment. Nothing is written outside this folder."))
                : null,

            props.building ? e(Working, { label: props.buildLabel }) : null,

            e(Problem, { message: props.error }),

            dataset && !props.building
                ? ui.div(null,
                    ui.div({ className: "card" },
                        ui.h2({ className: "card__heading" }, "What was planted"),
                        ui.p({ className: "card__body", style: { marginBottom: "20px" } },
                            "Each of these is something the agent is now expected to find. " +
                            "Anything it misses will be named on the next screen."),
                        ui.ul({ className: "planted" }, dataset.planted.map(function (item, index) {
                            return ui.li({ key: index, className: "planted__item" },
                                ui.span({ className: "planted__mark" }),
                                ui.span(null, item));
                        }))),

                    /* One bubble per file, so the data is understood before it
                       is looked at. The preview underneath is for anyone who
                       wants to check the wording for themselves. */
                    ui.div({ className: "card" },
                        ui.p({ className: "section-title" },
                            files.length === 1 ? "The file it was given"
                                               : "The " + files.length + " files it was given"),
                        ui.div({ className: "sources" }, files.map(function (item) {
                            return ui.div({ key: item.name, className: "source" },
                                ui.div({ className: "source__head" },
                                    ui.span({ className: "source__name" }, item.name),
                                    ui.span({ className: "source__meta" },
                                        count(item.rows) + " rows · " +
                                        count(item.columns.length) + " columns")),
                                ui.p({ className: "source__summary" },
                                    item.summary || item.label));
                        }))),

                    ui.div({ className: "card" },
                        ui.p({ className: "section-title" }, "Preview"),
                        ui.div({ className: "files" }, files.map(function (item, index) {
                            return ui.button({
                                key: item.name,
                                className: "file-tab",
                                type: "button",
                                "aria-selected": index === active,
                                onClick: function () { setActive(index); setMoreRows(null); }
                            }, item.name);
                        })),
                        preview ? e(Grid, { columns: preview.columns, rows: preview.rows }) : null,
                        file
                            ? ui.div({ className: "table-foot" },
                                ui.span(null, "Showing " + count(preview.rows.length) + " of " +
                                    count(file.rows) + " rows, " + count(file.columns.length) + " columns"),
                                preview.truncated
                                    ? ui.button({ className: "link", type: "button", onClick: showMore },
                                        "Show more")
                                    : null)
                            : null),

                    ui.div({ className: "actions" },
                        ui.button({ className: "btn btn--ghost", type: "button", onClick: props.onBack }, "Back"),
                        ui.button({ className: "btn btn--quiet", type: "button", onClick: props.onBuild },
                            "Build it again"),
                        ui.button({ className: "btn btn--primary", type: "button", onClick: props.onRun },
                            "Run the test")),
                    ui.p({ className: "hint" },
                        [ui.span({ key: "a" }, "Press "), e("kbd", { key: "b" }, "Enter"),
                         ui.span({ key: "c" }, " to run the test")]))
                : null);
    }

    // ----------------------------------------------------------------------
    // Screen three: the run
    // ----------------------------------------------------------------------

    /* The whole of the running screen. One sentence at a time in the middle,
       the sentences before it receding underneath, and four bars in the brand's
       colours to say that something is still going on. */
    function Stage(props) {
        var notes = props.notes;
        var latest = notes.length ? notes[notes.length - 1] : props.waiting;
        var trail = notes.slice(0, -1).slice(-4).reverse();
        /* An agent quoting its own figures can run long. Setting a wall of
           display type at 29px would be shouting, so it steps down instead. */
        var wordy = latest.length > 92;

        return ui.div({ className: "stage" },
            ui.p({ className: "stage__caption" }, props.caption),
            ui.div({ className: "stage__mark", "aria-hidden": "true" },
                [0, 1, 2, 3].map(function (index) {
                    return ui.span({ key: index, className: "stage__bar" });
                })),
            ui.h2({
                className: "stage__now" + (wordy ? " stage__now--wordy" : ""),
                "aria-live": "polite"
            }, ui.span({ key: latest }, latest)),
            trail.length
                ? ui.ul({ className: "stage__trail" }, trail.map(function (note, index) {
                    return ui.li({ key: note + index }, note);
                }))
                : null,
            ui.p({ className: "stage__clock" },
                props.finishedIn !== null
                    ? "The agent finished in " + props.finishedIn + " seconds"
                    : elapsedWords(props.seconds)),
            ui.div({ className: "stage__meter" }));
    }

    function classifyLine(line) {
        if (line.indexOf("$ ") === 0) { return "command"; }
        var upper = line.toUpperCase();
        if (upper.indexOf("ERROR") >= 0 || upper.indexOf("TRACEBACK") >= 0) { return "error"; }
        if (upper.indexOf("WARNING") >= 0) { return "warn"; }
        if (upper.indexOf("INFO") >= 0) { return "info"; }
        return "plain";
    }

    function Console(props) {
        return ui.div({ className: "console" },
            props.lines.length === 0
                ? ui.p({ className: "console__empty" }, "The agent printed nothing.")
                : props.lines.map(function (line, index) {
                    return ui.div({
                        key: index,
                        className: "console__line console__line--" + classifyLine(line)
                    }, line);
                }));
    }

    function Notes(props) {
        return ui.div({ className: "notes" },
            props.notes.length === 0
                ? ui.p({ className: "notes__empty" }, "There was nothing worth remarking on.")
                : props.notes.map(function (note, index) {
                    return ui.div({ key: index, className: "note" },
                        ui.span({ className: "note__rail" }, ui.span({ className: "note__dot" })),
                        ui.p({ className: "note__text" }, note));
                }));
    }

    /* Everything the run produced, folded away. It is evidence, and evidence
       should be kept and should not be the first thing anybody sees. */
    function Record(props) {
        var openState = useState("");
        var open = openState[0];
        var setOpen = openState[1];

        function tab(key, label) {
            return ui.button({
                key: key,
                className: "record__toggle",
                type: "button",
                "aria-expanded": open === key,
                onClick: function () { setOpen(open === key ? "" : key); }
            }, label);
        }

        return ui.div({ className: "record" },
            ui.div({ className: "record__tabs" },
                tab("steps", "Step by step"),
                tab("log", "Technical log · " + count(props.logs.length) + " lines")),
            open === "steps"
                ? ui.div({ className: "record__body" }, e(Notes, { notes: props.notes }))
                : null,
            open === "log"
                ? ui.div({ className: "record__body" }, e(Console, { lines: props.logs }))
                : null);
    }

    function Verdict(props) {
        var status = props.status;
        var words = { pass: "Passed", warn: "Passed with observations", fail: "Failed" };
        var verdict = props.verdict || {};

        return ui.div({ className: "verdict verdict--" + status },
            ui.p({ className: "verdict__badge" },
                ui.span({ className: "pulse pulse--" + (status === "pass" ? "done" : status) }),
                words[status]),
            ui.h2({ className: "verdict__headline" }, verdict.headline || ""),
            verdict.points && verdict.points.length
                ? ui.ul({ className: "verdict__points" }, verdict.points.map(function (point, index) {
                    return ui.li({ key: index }, point);
                }))
                : null,
            verdict.advice ? ui.p({ className: "verdict__advice" }, verdict.advice) : null,
            ui.p({ className: "verdict__author" },
                verdict.written_by === "model"
                    ? "Written by the language model from the evidence below."
                    : "Written by the harness from the evidence below."));
    }

    function Checks(props) {
        return ui.div({ className: "checks" }, props.checks.map(function (check, index) {
            return ui.div({ key: index, className: "check" },
                ui.span({ className: "check__pill check__pill--" + check.status },
                    check.status.toUpperCase()),
                ui.div(null,
                    ui.p({ className: "check__name" }, check.name),
                    check.measured ? ui.p({ className: "check__measured" }, check.measured) : null,
                    ui.p({ className: "check__detail" }, check.detail)));
        }));
    }

    function Outputs(props) {
        return ui.div({ className: "outputs" }, props.outputs.map(function (output) {
            return ui.div({ key: output.relative, className: "output" },
                ui.span({ className: "output__name" }, output.relative),
                ui.span({ className: "output__meta" },
                    output.rows
                        ? count(output.rows) + " rows · " + count(output.columns) + " columns · " +
                          bytes(output.bytes)
                        : bytes(output.bytes)),
                ui.button({
                    className: "link",
                    type: "button",
                    onClick: function () { props.onOpen(output.relative); }
                }, "Look inside"),
                ui.a({
                    className: "link",
                    href: withToken("/api/download?agent=" + encodeURIComponent(props.agent) +
                                    "&file=" + encodeURIComponent(output.relative))
                }, "Download"));
        }));
    }

    /* Rendered into the document body rather than in place. An overlay nested
       inside the page is at the mercy of whatever its ancestors do with
       transforms and filters, and will sooner or later be centred on something
       other than the window. */
    function Sheet(props) {
        useEffect(function () {
            function escape(event) { if (event.key === "Escape") { props.onClose(); } }
            window.addEventListener("keydown", escape);
            return function () { window.removeEventListener("keydown", escape); };
        }, [props.onClose]);

        return ReactDOM.createPortal(ui.div({
            className: "overlay",
            onClick: function (event) {
                if (event.target === event.currentTarget) { props.onClose(); }
            }
        },
            ui.div({ className: "sheet", role: "dialog", "aria-label": props.title },
                ui.div({ className: "sheet__head" },
                    ui.h2({ className: "sheet__title" }, props.title),
                    ui.button({ className: "close", type: "button", onClick: props.onClose,
                                "aria-label": "Close" }, "×")),
                ui.div({ className: "sheet__body" }, props.children))), document.body);
    }

    function TestScreen(props) {
        var state = useState({ logs: [], notes: [], phase: "Starting the agent",
                               running: true, result: null, error: "" });
        var run = state[0];
        var setRun = state[1];
        var tick = useState(0);
        var seconds = tick[0];
        var setSeconds = tick[1];
        var shownState = useState(0);
        var shown = shownState[0];
        var setShown = shownState[1];
        var looking = useState(null);
        var open = looking[0];
        var setOpen = looking[1];
        var contents = useState(null);
        var sheet = contents[0];
        var setSheet = contents[1];

        useEffect(function () {
            var source = new EventSource(withToken(
                "/api/run?agent=" + encodeURIComponent(props.agent.key) +
                "&use_model=" + (props.useModel ? "1" : "0")));

            source.addEventListener("log", function (event) {
                var line = JSON.parse(event.data).line;
                setRun(function (previous) {
                    return Object.assign({}, previous, { logs: previous.logs.concat([line]) });
                });
            });
            source.addEventListener("note", function (event) {
                var note = JSON.parse(event.data).note;
                setRun(function (previous) {
                    return Object.assign({}, previous, { notes: previous.notes.concat([note]) });
                });
            });
            source.addEventListener("phase", function (event) {
                var label = JSON.parse(event.data).label;
                setRun(function (previous) { return Object.assign({}, previous, { phase: label }); });
            });
            source.addEventListener("result", function (event) {
                var result = JSON.parse(event.data);
                setRun(function (previous) {
                    return Object.assign({}, previous, { result: result, running: false });
                });
            });
            source.addEventListener("failed", function (event) {
                var message = JSON.parse(event.data).message;
                setRun(function (previous) {
                    return Object.assign({}, previous, { error: message, running: false });
                });
            });
            source.addEventListener("end", function () {
                source.close();
                setRun(function (previous) { return Object.assign({}, previous, { running: false }); });
            });
            source.onerror = function () {
                source.close();
                setRun(function (previous) {
                    if (previous.result) { return Object.assign({}, previous, { running: false }); }
                    return Object.assign({}, previous, {
                        running: false,
                        error: "The connection to the server closed before the run finished."
                    });
                });
            };

            return function () { source.close(); };
        }, [props.agent, props.useModel]);

        /* A clock that only counts while the run is going. Without it a quiet
           stretch is indistinguishable from a stall. */
        useEffect(function () {
            if (!run.running) { return undefined; }
            var timer = window.setInterval(function () {
                setSeconds(function (value) { return value + 1; });
            }, 1000);
            return function () { window.clearInterval(timer); };
        }, [run.running]);

        /* The narration is revealed a sentence at a time rather than as fast as
           it arrives. On a few hundred rows these agents finish in well under a
           second, and an account of the run that appears and vanishes inside
           one frame is no account at all. The duration reported underneath and
           in the verdict is the measured one, so nothing here is overstated. */
        useEffect(function () {
            if (shown >= run.notes.length) { return undefined; }
            var timer = window.setTimeout(function () {
                setShown(function (value) { return value + 1; });
            }, shown === 0 ? 450 : 900);
            return function () { window.clearTimeout(timer); };
        }, [shown, run.notes.length]);

        var look = useCallback(function (relative) {
            setOpen(relative);
            setSheet(null);
            api("/api/output?agent=" + encodeURIComponent(props.agent.key) +
                "&file=" + encodeURIComponent(relative) + "&limit=40")
                .then(setSheet)
                .catch(function (error) { setSheet({ text: error.message }); });
        }, [props.agent]);

        var result = run.result;
        var status = result ? result.status : "running";
        /* Not "the agent has stopped" but "there is nothing left to show":
           the stage holds until the last sentence has had its moment. */
        var told = !run.running && shown >= run.notes.length;

        return ui.section({ className: "screen" },
            ui.div({ className: "screen__head" },
                ui.p({ className: "eyebrow" }, "Agent " + props.agent.number),
                ui.h1({ className: "title" }, props.agent.name),
                told && result
                    ? ui.p({ className: "subtitle" },
                        "Finished in " + result.seconds + " seconds. Everything below was " +
                        "measured against what was planted.")
                    : null),

            told ? e(Problem, { message: run.error }) : null,

            !told
                ? e(Stage, {
                    caption: run.running ? run.phase : "What happened",
                    notes: run.notes.slice(0, shown),
                    waiting: "Setting the agent going",
                    seconds: seconds,
                    finishedIn: result ? result.seconds : null
                })
                : null,

            told && result ? e(Verdict, { status: status, verdict: result.verdict }) : null,

            told && result
                ? ui.div({ className: "card", style: { marginTop: "16px" } },
                    ui.p({ className: "section-title" }, "What was checked"),
                    e(Checks, { checks: result.checks }))
                : null,

            told && result && result.outputs.length
                ? ui.div({ className: "card" },
                    ui.p({ className: "section-title" }, "What the agent wrote"),
                    e(Outputs, { outputs: result.outputs, agent: props.agent.key, onOpen: look }))
                : null,

            told && result && result.usage && (result.usage.requests || result.usage.failures)
                ? ui.div({ className: "card" },
                    ui.p({ className: "section-title" }, "Model usage"),
                    ui.p({ className: "card__body" },
                        count(result.usage.requests) + " requests, " +
                        count(result.usage.total_tokens) + " tokens, " +
                        "about $" + result.usage.estimated_cost_usd.toFixed(2) + " at the " +
                        "published rates. Cached answers were reused " +
                        count(result.usage.cache_hits) + " times at no cost."),
                    /* A run where the model was never reached looks exactly like
                       a run where it was, unless this is said out loud. */
                    result.usage.failures
                        ? ui.p({ className: "card__body", style: { marginTop: "8px" } },
                            count(result.usage.failures) + " request" +
                            (result.usage.failures === 1 ? " did" : "s did") +
                            " not come back, so those steps used the local rules instead. " +
                            "Check the key and the endpoint in .env.")
                        : null)
                : null,

            told ? e(Record, { notes: run.notes, logs: run.logs }) : null,

            ui.div({ className: "actions" },
                ui.button({
                    className: "btn btn--quiet",
                    type: "button",
                    disabled: !told,
                    onClick: props.onBack
                }, "Back to the data"),
                ui.button({
                    className: "btn btn--primary",
                    type: "button",
                    disabled: !told,
                    onClick: props.onRestart
                }, "Start over")),

            open
                ? e(Sheet, { title: open, onClose: function () { setOpen(null); } },
                    !sheet
                        ? e(Working, { label: "Reading the file" })
                        : (sheet.columns
                            ? e(Grid, { columns: sheet.columns, rows: sheet.rows })
                            : ui.pre({ className: "sheet__text" }, sheet.text || "")))
                : null);
    }

    // ----------------------------------------------------------------------
    // The application
    // ----------------------------------------------------------------------

    function App() {
        var screen = useState("gate");
        var step = screen[0];
        var setStep = screen[1];

        var catalogueState = useState(null);
        var catalogue = catalogueState[0];
        var setCatalogue = catalogueState[1];

        var chosenState = useState(null);
        var chosen = chosenState[0];
        var setChosen = chosenState[1];

        var datasetState = useState(null);
        var dataset = datasetState[0];
        var setDataset = datasetState[1];

        var buildingState = useState(false);
        var building = buildingState[0];
        var setBuilding = buildingState[1];

        var errorState = useState("");
        var error = errorState[0];
        var setError = errorState[1];

        var modelState = useState(false);
        var useModel = modelState[0];
        var setUseModel = modelState[1];

        var runKeyState = useState(0);
        var runKey = runKeyState[0];
        var setRunKey = runKeyState[1];

        /* The catalogue is fetched the moment the door opens, so the welcome
           covers the wait and the first screen is already there behind it. */
        var open = useCallback(function (token) {
            session = token;
            setStep("welcome");
            api("/api/agents")
                .then(setCatalogue)
                .catch(function (problem) { setError(problem.message); });
        }, []);

        var agent = catalogue && chosen
            ? catalogue.agents.filter(function (item) { return item.key === chosen; })[0]
            : null;

        var build = useCallback(function () {
            if (!chosen) { return; }
            setBuilding(true);
            setError("");
            setDataset(null);
            postJSON("/api/synthesise", { agent: chosen, use_model: useModel })
                .then(function (payload) { setDataset(payload); })
                .catch(function (problem) { setError(problem.message); })
                .then(function () { setBuilding(false); });
        }, [chosen, useModel]);

        var restart = useCallback(function () {
            postJSON("/api/reset", {}).catch(function () { /* nothing to recover */ });
            setDataset(null);
            setChosen(null);
            setError("");
            setStep("choose");
        }, []);

        /* Stable, because the welcome hangs its timers on it: a fresh function
           every render would restart the countdown on every render. */
        var enter = useCallback(function () { setStep("choose"); }, []);

        var goToData = useCallback(function () {
            if (chosen) { setStep("data"); }
        }, [chosen]);

        var goToTest = useCallback(function () {
            setRunKey(function (value) { return value + 1; });
            setStep("test");
        }, []);

        /* Enter always does the obvious next thing, so the whole flow can be
           driven without reaching for the mouse. */
        useEffect(function () {
            function onKey(event) {
                if (event.key !== "Enter" || event.metaKey || event.ctrlKey) { return; }
                var tag = (event.target.tagName || "").toLowerCase();
                if (tag === "input" || tag === "textarea") { return; }
                if (step === "choose" && chosen) { event.preventDefault(); goToData(); }
                else if (step === "data" && dataset && !building) { event.preventDefault(); goToTest(); }
                else if (step === "data" && !dataset && !building) { event.preventDefault(); build(); }
            }
            window.addEventListener("keydown", onKey);
            return function () { window.removeEventListener("keydown", onKey); };
        }, [step, chosen, dataset, building, goToData, goToTest, build]);

        if (step === "gate") {
            return ui.div({ className: "app" }, e(GateScreen, { onOpen: open }));
        }
        if (step === "welcome") {
            return ui.div({ className: "app" }, e(WelcomeScreen, { onDone: enter }));
        }

        if (!catalogue) {
            return ui.div({ className: "app" },
                e(Masthead, { step: step }),
                ui.main({ className: "main" },
                    error ? e(Problem, { message: error }) : e(Working, { label: "Starting up" })));
        }

        var body;
        if (step === "choose") {
            body = e(ChooseScreen, {
                agents: catalogue.agents,
                chosen: chosen,
                onChoose: function (key) {
                    if (key !== chosen) { setDataset(null); }
                    setChosen(key);
                },
                onNext: goToData
            });
        } else if (step === "data") {
            body = e(DataScreen, {
                agent: agent,
                model: catalogue.model,
                useModel: useModel,
                onModel: setUseModel,
                dataset: dataset,
                building: building,
                buildLabel: useModel
                    ? "Inventing the data, with the model widening the vocabulary"
                    : "Inventing the data",
                error: error,
                onBuild: build,
                onBack: function () { setStep("choose"); },
                onRun: goToTest
            });
        } else {
            body = e(TestScreen, {
                key: runKey,
                agent: agent,
                useModel: useModel,
                onBack: function () { setStep("data"); },
                onRestart: restart
            });
        }

        return ui.div({ className: "app" },
            e(Masthead, { step: step }),
            ui.main({ className: "main" }, body),
            ui.footer({ className: "colophon" },
                "Agent test harness " + catalogue.version,
                ui.span(null, "·"),
                "Synthetic data only",
                ui.span(null, "·"),
                "Prof. Shahab Anbarjafari"));
    }

    ReactDOM.createRoot(document.getElementById("root")).render(e(App));
}());
