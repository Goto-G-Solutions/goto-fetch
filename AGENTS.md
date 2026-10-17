# How I work, and how I want code written

This file tells you how I learn, what I mean by good code and good design, and how we work together.
Reply to me in plain language (ISO 24495-1:2023) unless I ask for something else.
Comments, doc strings, commit messages and Markdown files follow the same rule.


## 1. How I learn

I understand things best from the ground up: the idea and the reason first, then the details.
Once I see the "why" and the "how", formulas and intricate code are much easier to take in.

When we take on a new topic or tool, we work in small steps:

1. **Stop and externalize.** I say what I understand and what I want to reach. You correct any mistakes and guide me in small, intuitive steps, so the foundation is sound.
2. **Reduce scope.** We pick one small, specific piece, on your advice, and finish it.
3. **Scaffolding.** You give me short, clear examples to refer to as I work.
4. **Permission to be messy.** First make it work, then make it good. Mark what needs more work with `NOTE`, `TODO` or `FIXME`, so nothing is lost.


## 2. Designing systems

I design systems the way Dijkstra wrote programs: start from a vague goal at the top, state the constraints, and refine one level at a time until every part is precise.

### From vague to precise

1. **State the purpose in one sentence.** If it needs two, it may be two systems.
2. **State the constraints.** List what must always be true (invariants), what the system must never do, and the limits it works within, such as size, speed, permissions and platforms.
3. **Refine one level at a time.** Split the current level into a few parts, and say exactly what each part promises before designing its inside. Do not reach down two levels at once.
4. **Keep each step correct.** Each refinement must keep every promise of the level above. If it cannot, fix the level above first.
5. **Stop when the parts are obvious.** A part is done when its code is plain to write and plain to check.

If the goal is too unclear to state, build a throwaway spike first: the smallest thing that answers the open question. Then design top-down from what it taught, and do not grow the spike into the real thing.

### Systems as sets

I picture a system as a diagram of sets.

- **A circle is one engine.** Each circle holds one capability, such as storage, parsing, scheduling or rendering, and everything that capability needs.
- **The boundary is the interface.** Other circles touch an engine only through its interface. The interface is the abstraction; the inside can change freely behind it.
- **Overlap is coupling.** Where two circles overlap, two engines share a responsibility. Make each overlap small and deliberate, or redraw the circles so it goes away.
- **The ideal is concentric circles.** Each circle wraps the one inside it and builds only on that circle's interface. Dependencies point inward. The core holds the smallest, most stable primitives; each outer circle composes them into something larger, and exposes an interface that is no larger than it needs to be.
- **Concerns that touch every circle are passed in.** Logging, configuration, errors, cancellation and permissions reach every circle. Pass them in from the outside, as parameters or a context, rather than drawing them as circles of their own.
- **Scale the circles to the problem.** In small code a circle can be one function, and its interface is that function's signature. Do not add layers, types or interfaces that the problem does not have.

### Composable primitives

The goal for each interface is the smallest set of primitives that compose to cover everything the circle is for.

- **Orthogonal.** Each primitive does one thing that no other primitive does. Two ways to do the same thing is a design smell.
- **Uniform.** Primitives take and return the same kinds of values, and report errors the same way, so the output of one can feed another without glue.
- **Closed under composition.** Combining primitives gives something the interface already understands, so compositions can be composed again, like Unix pipes.
- **Effects at the edge.** Keep the core free of side effects where you can: input, output, time and randomness enter at the outermost circle and are passed inward.
- **Replaceable.** Anything that implements an interface can stand in for another implementation, and callers cannot tell the difference.

Questions to ask of any design:

- What is the engine of this circle, in one sentence?
- What is its interface, and could it be smaller?
- Does any circle reach past its neighbour to touch something deeper?
- Can each new feature be built by composing existing primitives? If not, is a primitive missing, or is the feature in the wrong circle?

```go
// Good: tiny interfaces, composed freely. Anything that reads can feed
// anything that writes, and neither knows what the other is.
type Reader interface{ Read(p []byte) (n int, err error) }
type Writer interface{ Write(p []byte) (n int, err error) }

// io.Copy(dst, src) composes any Writer with any Reader:
// a file, a network socket, a gzip stream, or a test buffer.
```


## 3. Languages

- **Python** is my general default.
- **R** is for statistics and data science.
- **Go** is for utilities and services.
- **Shell** (bash, zsh) is for glue and machine setup.

In every language:

- Give each public function, type and module a doc string or doc comment.
- Add comments where they help a reader. See section 9 for their style.


## 4. Elegant code

Elegant code has six qualities. When two of them conflict, the one earlier in the list wins.

1. **Principled.** It follows the system's design (section 2): it lives in the right circle, talks to other circles only through their interfaces, and keeps every stated invariant.
2. **Capable.** It does the whole job, and does it correctly, including the hard cases. Code that is short and clear but wrong in an edge case is not elegant.
3. **Readable.** A reader sees what it does and why. Names say what things are. The shape of the code follows the shape of the problem.
4. **Performant.** It uses time and memory in proportion to the work, at the sizes we expect and beyond them. The right data structure and algorithm come first; tuning comes after measuring, and its result is measured too. No step should cost far more than what it changes.
5. **Simple.** It has the fewest moving parts that do the job. Build only what the current problem needs, not what a future one might (YAGNI). Complexity must earn its place (KISS).
6. **Not repeated.** Each piece of knowledge, such as a rule, a limit or a format, has one home (DRY). Two pieces of code that look alike but mean different things are not repetition. Do not merge them.

How to break ties:

- **Principled over capable:** if handling a case seems to need a hole in a boundary, the boundary is probably in the wrong place. Redraw it; do not punch through it.
- **Capable over readable:** handle the edge case, then make the handling clear.
- **Readable over performant:** fast code nobody can read cannot be safely changed. Make the fast version readable (a named helper, a comment on why it is shaped so), or keep the plain one until a measurement says it matters.
- **Performant over simple:** a cache, an index or a checkpoint may earn its place, once a measurement shows the plain way costs too much. Keep the measurement as a benchmark, so the reason stays checkable.
- **Simple over not repeated:** repeat a small thing twice before building an abstraction for it. Abstract on the third time, or when the repeated thing is real domain knowledge.

```go
// Good: one home for the rule, and the name says what it is.
const maxNameLength = 200

func validName(s string) bool {
	return s != "" && len(s) <= maxNameLength
}
```


## 5. Priorities: safety, performance, developer experience

Section 4 ranks qualities of the code. This section ranks goals for what the code does, and safety outranks everything, including "principled".

In this order:

1. **Safety.** The code works in every case it can meet. It fails in a controlled way, never in a way that corrupts data or hides the cause.
2. **Performance.** Treat speed and resource use as a design constraint from the start. Choose the right data structures and algorithms up front. Measure before you tune, and keep measuring, with benchmarks for the hot paths.
3. **Developer experience.** Clear structure, good names and consistent patterns, so people spend their time on the problem, not on decoding the code.

Design principles that serve these (SOLID, briefly). Each one is a rule about circles and their interfaces:

- Each unit has one reason to change.
- Extend behaviour by adding code, not by editing code that others depend on and that is tested and working. Code that only we use, and that is not released, can simply be fixed.
- Anything that stands in for another must behave like it.
- Keep interfaces small. No caller should depend on methods it does not use.
- High-level logic depends on abstractions, not on storage, network or user-interface details.

Avoid the common traps:

- **God objects:** one type that does everything. Split it by responsibility.
- **Sequential coupling:** methods that must be called in a fixed order. Wrap the sequence in one method.
- **Feature envy:** code that mostly works with another type's data. Move it to that type.


## 6. Understand before changing

- **Context first.** Understand the architecture and the intent of the code before you change it. A fix made without that often causes new bugs.
- **Impact.** Check what depends on the code you change, and what it depends on.
- **History.** Read the git history and the docs to learn why the code is as it is.
- **Record decisions.** Write down the reason behind significant changes, in the commit body or in `docs/`.
- **No technical debt.** Code is cheapest to fix while you are still working on it. Do it right before moving on. Tidy the code as you go, without changing what it does.
- **Stay in scope.** Do what was asked, fully. Changes beyond it, such as redrawing a boundary, a wider refactor or a new dependency, are proposed first with the reason, not just made.


## 7. Errors, resources and security

- **Fail fast.** Find problems as early as possible. Check assumptions and stop on a broken one.
- **Explicit errors.** Return errors and handle them. Do not hide them, and do not crash on input you could have expected. Keep the error's code or kind as it passes through layers.
- **Clean up resources.** Tie a resource's lifetime to a scope, such as `defer` in Go, `with` in Python, or `trap` in shell.
- **Validate at boundaries.** Treat all outside data as untrusted: user input, files, the network, and other processes. Allow only what is known to be good, rather than blocking what is known to be bad.
- **Least privilege.** Grant the smallest permission that does the job. Deny by default.
- **Defence in depth.** Do not rely on one check.
- **Batch reads.** Fetch related records in one query, not one query per item (the N+1 problem).
- **Few dependencies.** Each one is a risk and a cost. Pin versions and remove what is not used.


## 8. Testing

I use a relaxed form of test-driven development.

- **What to test:** the hot paths, every regression, and the edge cases.
- **Where to test:** at the interfaces. A circle's tests describe the promises its interface makes, so the inside can change without rewriting them.
- **When:** as you write the code, cover the hot paths and the obvious edge cases. Add a test for each new edge case or bug as you find it.
- **Tests as documentation:** tidy the tests as the code settles, so they describe what the software does. Test names read as statements of behaviour, for example `TestRenameSkipsAnExistingTarget`.
- **Both sides:** test what must happen, and what must not. For example, that a refused call is refused, and that nothing was written.
- **Exact assertions:** where it helps, assert the exact value, error code or count, not just "no error". A test should prove the code does exactly what we expect.

Each project has one command that runs every test, named in its README.


## 9. How we work

**Commits**

- Form: `type(scope): lowercase summary`, for example `fix(parser): reject empty names`.
- The body explains why the change was made, not only what changed.
- Every commit builds and passes the project's test command.
- Commit as we go, in small steps that each make sense alone.

**Comments**

- Present tense, short.
- Say what the code does and why, and warn about traps (footguns).
- Do not narrate history ("changed from X", "used to"). Git records that.

**Compatibility**

- No backwards compatibility until something is released. Change names, formats and APIs freely, and update every caller.

**Reports**

- Report results honestly: what ran, what passed, what failed, and what did not run.


## 10. Guides

If the project has a `docs/guide/` folder, it holds working notes and best practices for each area, such as a framework or a tool. They work like skills.

- Before working in an area, read its guide. The index is `docs/guide/README.md`.
- When work teaches something new about an area, add it to that guide in the same commit.


## 11. Pair programming and debugging

Often you (the driver) know more about the code than I do (the navigator). Your job is to give me enough context to make good decisions.

**As the driver:**

1. **Start with why.** Explain what the code is for and what problem it solves, before how it works.
2. **Give context, not a flood.** Start with a high-level overview. Go into detail when I ask.
3. **State the expected behaviour:** the input, what happens to it, and the output or side effects.
4. **Name the connections:** the files, modules and systems the code depends on or affects.
5. **Point out pitfalls and assumptions** that are not obvious.
6. **Give your opinion.** Recommend a solution and explain the pros and cons of the options.
7. **Invite questions.** Ask what I think, and how I would approach it.

**A session runs like this:**

1. **Problem or goal** (me): "I see X when I do Y", or "I want Z".
2. **Overview** (you): the relevant part of the system, its purpose and its expected behaviour.
3. **Walkthrough** (you, guided by my questions): the code in question, and why it is written that way.
4. **Hypotheses** (together): theories for the bug, or approaches to the feature.
5. **Options** (together): possible fixes, with pros and cons.
6. **Decision** (together): the path we take, and its steps.
7. **Verification:** how we will know it works, such as a test, a manual check, or a log line.


## 12. This project

**Purpose.** `api_explorer` pulls data from paginated JSON APIs and shapes it for any dataframe library, with a marimo notebook for exploring an API and copying the fetch as a script.

**Layers.** Each module imports only from modules listed before it. Dependencies point inward, as in section 2.

1. `errors`, `paths`, `decode`: the exception, reading values by path, and parsing JSON.
2. `models`, `records`, `retry`: typed records, reshaping records, and the retry policy.
3. `frames`, `paging`: dataframes, and the paging loop and styles.
4. `auth`, `client`: authentication, and the HTTP client.
5. `scrape`, `plan`: HTML scraping, and describing a fetch as a script.

**Dependencies.**

- The core needs only `httpx2` and `narwhals`.
- Anything else is an optional extra (`scrape`, `explorer`).
- An optional package is imported only in the module that needs it, never in `__init__.py`, so the core runs without it.
- **Extension points.** Each piece a user might replace sits behind a small interface, and the built-in version is one implementation of it: `decode` (JSON parsing), `model` (typed records), `backend` (the dataframe library), `RetryPolicy` (built in: `Retry`), `Paging` (built in: `NextLink`, `Offset`, `HeaderLink`), `httpx2.Auth`, and `http` (an httpx2 client of the user's own).
- **Built from public parts.** `Client.records` is composed of public pieces (`paginate`, a `Paging`, `read_page`, `converter`), so a user's own loop can reuse them. Keep it that way.
- **One place for each setting.** When two ways to set the same thing meet, such as `Client(timeout=...)` and the timeout of a client passed as `http`, refuse the pair with a clear error instead of picking one.
- An upgrade happens by itself only if it gives exactly the same results, such as msgspec for JSON. Anything that can change results is chosen by name, such as orjson, which turns integers beyond 64 bits into floats.

**Commands.**

- Tests: `uv run pytest`. This is the one command that runs every test.
- Lint: `uvx ruff check` and `uvx ruff format --check`.
- Types: `uv run pyright`. The package ships its type hints (`py.typed`), so they must be right.
- The notebook: `uvx marimo check --strict notebooks/explorer.py`.
- The notebook's formatting belongs to marimo, which formats it on save, so `ruff format` leaves it alone.

**Tests.** These add to section 8.

- **Orthogonal.** Each test checks one behaviour, and no two tests check the same thing. When two tests do overlap, a comment in the later one says why the overlap is worth having.
- **Bugs.** Every bug fix comes with a regression test, marked with a comment that starts `# Regression:` and says what used to go wrong.
- **Features.** Every new feature comes with tests at its interface.
- **Reasonable.** Don't test what the language or a library already guarantees, such as a dataclass storing its fields.
- **Dataframes.** Frame tests take the `backend` fixture, so they run once per installed library.
- **Fakes.** Fake servers live in `tests/support.py`. Tests never touch the network.

**Writing.** Plain language, as at the top of this file. No em dashes, anywhere: code, comments, docs, commits, issues or pull requests.

**Issues and pull requests.** Every pull request starts from an issue, and links it. See `CONTRIBUTING.md`.
