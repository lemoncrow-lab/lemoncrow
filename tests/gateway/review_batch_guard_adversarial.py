"""Cases a reviewer found that got past the guard, each a real file on both sides (adv5 to adv17).

``HELD`` must all be held, with the whole file on both sides and with the context that blinds the hunk reading in
place. ``OFFERED`` are the documented limits (SPEC 7.1 P4-2 L): a statement that crosses a block and also changes its
shape (T7, T15, Z17, Z18, G5), a guard clause inserted above a statement (V8), and a decorator taken off (F3b, F6): a
removal is not a move, and the guard does not hold removals. ``FREE`` are changes that move nothing and must stay
offered: line endings and separators that shift a line count when the text is split carelessly, and a parameter
added in front of a decorated one.
"""

HELD = {
    "T1 js block-comment phantom { between, loop out": (
        "a.ts",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n    send(x);\n    /*\n      if (a) {\n    */\n  }\n  done();\n}\n",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n    /*\n      if (a) {\n    */\n  }\n  send(x);\n  done();\n}\n",
    ),
    "T2 js jsx url text drops }": (
        "a.tsx",
        "function f(xs, x, ok) {\n  for (const a of xs) {\n    keep(a);\n    send(x);\n    out.push(<p>{ok && <a>see http://example.com</a>}</p>);\n  }\n  done();\n}\n",
        "function f(xs, x, ok) {\n  for (const a of xs) {\n    keep(a);\n    out.push(<p>{ok && <a>see http://example.com</a>}</p>);\n  }\n  send(x);\n  done();\n}\n",
    ),
    "T3 js regex /\\{/ phantom": (
        "a.ts",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n    send(x);\n    const open = /\\{/;\n  }\n  done();\n}\n",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n    const open = /\\{/;\n  }\n  send(x);\n  done();\n}\n",
    ),
    "T4 js semicolon dropped on move out of loop": (
        "a.ts",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n    send(x);\n  }\n  done();\n}\n",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n  }\n  send(x)\n  done();\n}\n",
    ),
    "T5 py magic trailing comma collapse on move out": (
        "a.py",
        "def f(xs, x, y):\n    for a in xs:\n        keep(a)\n        send(\n            x,\n            y,\n        )\n    done()\n",
        "def f(xs, x, y):\n    for a in xs:\n        keep(a)\n    send(x, y)\n    done()\n",
    ),
    "T5b py black rhs wrap (no trailing comma) on move in": (
        "a.py",
        "def f(xs, x, y):\n    for a in xs:\n        keep(a)\n    send(x, y)\n    done()\n",
        "def f(xs, x, y):\n    for a in xs:\n        keep(a)\n        send(\n            x, y\n        )\n    done()\n",
    ),
    "T6 js prettier wrap on move into loop": (
        "a.ts",
        "function f(xs, alpha, beta) {\n  for (const a of xs) {\n    keep(a);\n  }\n  send(alpha, beta);\n  done();\n}\n",
        "function f(xs, alpha, beta) {\n  for (const a of xs) {\n    keep(a);\n    send(\n      alpha,\n      beta,\n    );\n  }\n  done();\n}\n",
    ),
    "T6b js prettier wrap no trailing comma (same row tokens split)": (
        "a.ts",
        "function f(xs, alpha, beta) {\n  for (const a of xs) {\n    keep(a);\n  }\n  send(alpha, beta);\n  done();\n}\n",
        "function f(xs, alpha, beta) {\n  for (const a of xs) {\n    keep(a);\n    send(\n      alpha, beta);\n  }\n  done();\n}\n",
    ),
    "T8 js in-place same-skeleton swap": (
        "a.ts",
        "function f(a) {\n  if (a) { x(); }\n  z();\n}\n",
        "function f(a) {\n  if (a) { y(); }\n  x();\n}\n",
    ),
    "T9 js arrow one-liner move into callback": (
        "a.ts",
        "function f(xs, x) {\n  xs.forEach((a) => { keep(a) });\n  send(x);\n}\n",
        "function f(xs, x) {\n  xs.forEach((a) => { keep(a); send(x) });\n}\n",
    ),
    "T10 js generic return one-liner fn body moved": (
        "a.ts",
        "function f(x) {\n  const g = (): Promise<void> => { return run() };\n  send(x);\n}\n",
        "function f(x) {\n  const g = (): Promise<void> => { send(x); return run() };\n}\n",
    ),
    "T11 js split run: open in one run, close in another": (
        "a.ts",
        "function f(ok, x) {\n  prep();\n  send(x);\n  log(x);\n  done();\n}\n",
        "function f(ok, x) {\n  prep();\n  if (ok) { mark();\n  send(x);\n  log(x);\n  } tidy();\n  done();\n}\n",
    ),
    "T12 py d rename on the way": (
        "a.py",
        "def f(xs, x):\n    for a in xs:\n        keep(a)\n        send(x)\n    done()\n",
        "def f(xs, x):\n    for a in xs:\n        keep(a)\n    send(y)\n    done()\n",
    ),
    "T13 py split into two on the way": (
        "a.py",
        "def f(xs, x):\n    for a in xs:\n        keep(a)\n        send(prep(x))\n    done()\n",
        "def f(xs, x):\n    for a in xs:\n        keep(a)\n    p = prep(x)\n    send(p)\n    done()\n",
    ),
    "T14 js merged on the way": (
        "a.ts",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n    const p = prep(x);\n    send(p);\n  }\n  done();\n}\n",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n  }\n  send(prep(x));\n  done();\n}\n",
    ),
    "T16 js block comment phantom } (closes early, move within)": (
        "a.ts",
        "function f(xs, x) {\n  for (const a of xs) {\n    send(x);\n    keep(a);\n    /*\n      }\n    */\n    more(a);\n  }\n  done();\n}\n",
        "function f(xs, x) {\n  for (const a of xs) {\n    keep(a);\n    /*\n      }\n    */\n    send(x);\n    more(a);\n  }\n  done();\n}\n",
    ),
    "U1 if-out, block comment with { inside": (
        "a.tsx",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    /*\n      if (x) {\n    */\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    /*\n      if (x) {\n    */\n  }\n  send(x);\n  done();\n}\n",
    ),
    "U2 if-out, jsx url text": (
        "a.tsx",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    out.push(<p>{ok && <a>see http://example.com</a>}</p>);\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    out.push(<p>{ok && <a>see http://example.com</a>}</p>);\n  }\n  send(x);\n  done();\n}\n",
    ),
    "U3 if-out, regex /\\{/": (
        "a.tsx",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    const open = /\\{/;\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    const open = /\\{/;\n  }\n  send(x);\n  done();\n}\n",
    ),
    "U4 if-out, string-ish regex /'{/": (
        "a.tsx",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    const re = /[{]/;\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    const re = /[{]/;\n  }\n  send(x);\n  done();\n}\n",
    ),
    "U5 if-out, block comment one row /* { */ (control)": (
        "a.tsx",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    /* { */\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    /* { */\n  }\n  send(x);\n  done();\n}\n",
    ),
    "U6 if-out, regex with // inside /a\\/\\/{/": (
        "a.tsx",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    const re = /\\/\\/b/; const o = {\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    const re = /\\/\\/b/; const o = {\n  }\n  send(x);\n  done();\n}\n",
    ),
    "U7 js semicolon dropped, if-out": (
        "a.ts",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n  }\n  send(x)\n  done();\n}\n",
    ),
    "U8 js prettier wrap into if": (
        "a.ts",
        "function f(ok, alpha, beta) {\n  if (ok) {\n    keep();\n  }\n  send(alpha, beta);\n  done();\n}\n",
        "function f(ok, alpha, beta) {\n  if (ok) {\n    keep();\n    send(\n      alpha,\n      beta,\n    );\n  }\n  done();\n}\n",
    ),
    "U9 py trailing comma collapse, if-out": (
        "a.py",
        "def f(ok, x, y):\n    if ok:\n        keep()\n        send(\n            x,\n            y,\n        )\n    done()\n",
        "def f(ok, x, y):\n    if ok:\n        keep()\n    send(x, y)\n    done()\n",
    ),
    "U10 js object-brace phantom: `const o = {` ctx w/o close": (
        "a.ts",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    // const o = {\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    // const o = {\n  }\n  send(x);\n  done();\n}\n",
    ),
    "V1 py one-line case arm: body swapped, send moved out": (
        "a.py",
        "def f(k, x, y):\n    match k:\n        case 1: send(x)\n        case 2: other()\n    done()\n",
        "def f(k, x, y):\n    match k:\n        case 1: keep(y)\n        case 2: other()\n    send(x)\n    done()\n",
    ),
    "V1b py one-line case arm -> pass + send out": (
        "a.py",
        "def f(k, x):\n    match k:\n        case 1: send(x)\n        case 2: other()\n    done()\n",
        "def f(k, x):\n    match k:\n        case 1: pass\n        case 2: other()\n    send(x)\n    done()\n",
    ),
    "V2 py exploded trailing comma, move into if": (
        "a.py",
        "def f(ok, alpha, beta, gamma):\n    if ok:\n        keep()\n    send(alpha, beta, gamma)\n    done()\n",
        "def f(ok, alpha, beta, gamma):\n    if ok:\n        keep()\n        send(\n            alpha,\n            beta,\n            gamma,\n        )\n    done()\n",
    ),
    "V3 py exploded trailing comma, move into loop": (
        "a.py",
        "def f(xs, alpha, beta, gamma):\n    for a in xs:\n        keep(a)\n    send(alpha, beta, gamma)\n    done()\n",
        "def f(xs, alpha, beta, gamma):\n    for a in xs:\n        keep(a)\n        send(\n            alpha,\n            beta,\n            gamma,\n        )\n    done()\n",
    ),
    "V4 py remove whole if-run so ctx falls into loop": (
        "a.py",
        "def f(xs, ok, x):\n    for a in xs:\n        keep(a)\n    if ok:\n        prep()\n        send(x)\n    done()\n",
        "def f(xs, ok, x):\n    for a in xs:\n        keep(a)\n        send(x)\n    done()\n",
    ),
    "V5 js prettier unwrap on move out of if": (
        "a.ts",
        "function f(ok, alpha, beta) {\n  if (ok) {\n    keep();\n    send(\n      alpha,\n      beta,\n    );\n  }\n  done();\n}\n",
        "function f(ok, alpha, beta) {\n  if (ok) {\n    keep();\n  }\n  send(alpha, beta);\n  done();\n}\n",
    ),
    "V6 js multi-row stmt moved verbatim (control)": (
        "a.ts",
        "function f(ok, alpha, beta) {\n  if (ok) {\n    keep();\n    send(\n      alpha,\n      beta,\n    );\n  }\n  done();\n}\n",
        "function f(ok, alpha, beta) {\n  if (ok) {\n    keep();\n  }\n  send(\n    alpha,\n    beta,\n  );\n  done();\n}\n",
    ),
    "V7 js multi-row block comment unbalanced { (realistic commented header)": (
        "a.ts",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    /* disabled for now:\n    if (legacy) {\n    */\n  }\n  done();\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    /* disabled for now:\n    if (legacy) {\n    */\n  }\n  send(x);\n  done();\n}\n",
    ),
    "W1 py if one-liner body swapped + send out": (
        "a.py",
        "def f(ok, x, y):\n    if ok: send(x)\n    done()\n",
        "def f(ok, x, y):\n    if ok: keep(y)\n    send(x)\n    done()\n",
    ),
    "W2 py else one-liner body swapped + send out": (
        "a.py",
        "def f(ok, x, y):\n    if ok: a()\n    else: send(x)\n    done()\n",
        "def f(ok, x, y):\n    if ok: a()\n    else: keep(y)\n    send(x)\n    done()\n",
    ),
    "W3 js case one-liner body swapped + send out": (
        "a.ts",
        "function f(k, x, y) {\n  switch (k) {\n    case 1: send(x); break;\n    case 2: other(); break;\n  }\n  done();\n}\n",
        "function f(k, x, y) {\n  switch (k) {\n    case 1: keep(y); break;\n    case 2: other(); break;\n  }\n  send(x);\n  done();\n}\n",
    ),
    "W4 js if one-liner body swapped + send out": (
        "a.ts",
        "function f(ok, x, y) {\n  if (ok) send(x);\n  done();\n}\n",
        "function f(ok, x, y) {\n  if (ok) keep(y);\n  send(x);\n  done();\n}\n",
    ),
    "W5 js braced one-liner body swapped + send out": (
        "a.ts",
        "function f(ok, x, y) {\n  if (ok) { send(x); }\n  done();\n}\n",
        "function f(ok, x, y) {\n  if (ok) { keep(y); }\n  send(x);\n  done();\n}\n",
    ),
    "W6 py case one-liner, send moved INTO arm": (
        "a.py",
        "def f(k, x, y):\n    match k:\n        case 1: keep(y)\n    send(x)\n    done()\n",
        "def f(k, x, y):\n    match k:\n        case 1: send(x)\n    keep(y)\n    done()\n",
    ),
    "W7 py with one-liner body swapped + send out": (
        "a.py",
        "def f(lk, x, y):\n    with lk: send(x)\n    done()\n",
        "def f(lk, x, y):\n    with lk: keep(y)\n    send(x)\n    done()\n",
    ),
    "X1 js leading-* operator ctx row swallows rest as comment": (
        "a.ts",
        "function f(ok, x, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n    send(x);\n  }\n  done();\n}\n",
        "function f(ok, x, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n  }\n  send(x);\n  done();\n}\n",
    ),
    "X2 js hunk starts and ends inside templates": (
        "a.ts",
        "const css = `\n  .a { color: red; }\n  .b {\n  }\n`;\nif (ok) {\n  send(x);\n}\ndone();\nconst sql = `\n  SELECT\n  FROM t\n  WHERE\n`;\n",
        "const css = `\n  .a { color: red; }\n  .b {\n  }\n`;\nif (ok) {\n}\nsend(x);\nconst sql = `\n  SELECT\n  FROM t\n  WHERE\n`;\n",
    ),
    'X3 py hunk starts in docstring closed by \'text"""\', sibling ifs': (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        send(x)\n        one()\n    if b:\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        send(x)\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
    ),
    "X4 js dynamic import().then callback: move out": (
        "a.ts",
        'function f(x) {\n  prep();\n  import("./mod").then((m) => {\n    m.init();\n    send(x);\n  });\n  done();\n}\n',
        'function f(x) {\n  prep();\n  import("./mod").then((m) => {\n    m.init();\n  });\n  send(x);\n  done();\n}\n',
    ),
    "X5 js callback param named type: move+rename out (d exemption)": (
        "a.ts",
        "function f(items, x, y) {\n  items.forEach((type) => {\n    keep(type);\n    send(x);\n  });\n  done();\n}\n",
        "function f(items, x, y) {\n  items.forEach((type) => {\n    keep(type);\n  });\n  send(y);\n  done();\n}\n",
    ),
    "X5b control: callback param named t: move+rename out": (
        "a.ts",
        "function f(items, x, y) {\n  items.forEach((t) => {\n    keep(t);\n    send(x);\n  });\n  done();\n}\n",
        "function f(items, x, y) {\n  items.forEach((t) => {\n    keep(t);\n  });\n  send(y);\n  done();\n}\n",
    ),
    "X6 js no-semi, ctx row ends with JSX /> (merges next stmt)": (
        "a.tsx",
        "function f(ok, x) {\n  if (ok) {\n    const el = <Icon />\n    send(x)\n  }\n  done()\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    const el = <Icon />\n  }\n  send(x)\n  done()\n}\n",
    ),
    "X7 py case with guard one-liner, send out": (
        "a.py",
        'def f(k, x, y):\n    match k:\n        case {"a": 1} if y: send(x)\n        case _: other()\n    done()\n',
        'def f(k, x, y):\n    match k:\n        case {"a": 1} if y: keep(y)\n        case _: other()\n    send(x)\n    done()\n',
    ),
    "X8 py if header with lambda colon one-liner, send out": (
        "a.py",
        "def f(ok, x, y):\n    if (lambda: ok)(): send(x)\n    done()\n",
        "def f(ok, x, y):\n    if (lambda: ok)(): keep(y)\n    send(x)\n    done()\n",
    ),
    "X9 js no-semi ctx row ends with generic type (merges)": (
        "a.ts",
        "function f(ok, x) {\n  if (ok) {\n    let a: Array<string>\n    send(x)\n  }\n  done()\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    let a: Array<string>\n  }\n  send(x)\n  done()\n}\n",
    ),
    "X10 js URL regex with escaped slashes ctx then brace": (
        "a.ts",
        "function f(ok, x, u) {\n  if (ok) {\n    keep();\n    send(x);\n    const isUrl = /^https?:\\/\\//; const o = {\n    };\n  }\n  done();\n}\n",
        "function f(ok, x, u) {\n  if (ok) {\n    keep();\n    const isUrl = /^https?:\\/\\//; const o = {\n    };\n  }\n  send(x);\n  done();\n}\n",
    ),
    "Y1 SPEC example: send(x) into a loop as send(x, retry=True)": (
        "a.py",
        "def f(xs, x):\n    for a in xs:\n        keep(a)\n    send(x)\n    done()\n",
        "def f(xs, x):\n    for a in xs:\n        keep(a)\n        send(x, retry=True)\n    done()\n",
    ),
    "Y2 X5 with a loop: forEach((type) => ...) move+rename out": (
        "a.ts",
        "function f(items, x, y) {\n  for (const type of items) {\n    keep(type);\n    send(x);\n  }\n  done();\n}\n",
        "function f(items, x, y) {\n  for (const type of items) {\n    keep(type);\n  }\n  send(y);\n  done();\n}\n",
    ),
    "Y3 X1 into a for loop": (
        "a.ts",
        "function f(xs, x, w, h) {\n  const area = w\n    * h;\n  for (const a of xs) {\n    keep(a);\n  }\n  send(x);\n  done();\n}\n",
        "function f(xs, x, w, h) {\n  const area = w\n    * h;\n  for (const a of xs) {\n    keep(a);\n    send(x);\n  }\n  done();\n}\n",
    ),
    "Z1 js return 'a' in if -> return 'b' after (no-ID shape)": (
        "a.ts",
        'function f(ok) {\n  if (ok) {\n    log();\n    return "a";\n  }\n  done();\n}\n',
        'function f(ok) {\n  if (ok) {\n    log();\n  }\n  return "b";\n  done();\n}\n',
    ),
    "Z1p py return 'a' in if -> return 'b' after": (
        "a.py",
        "def f(ok):\n    if ok:\n        log()\n        return 'a'\n    done()\n",
        "def f(ok):\n    if ok:\n        log()\n    return 'b'\n    done()\n",
    ),
    "Z1q py break moved out of if into loop body (no-ID, same key)": (
        "a.py",
        "def f(xs):\n    for a in xs:\n        if a:\n            log(a)\n            break\n        use(a)\n",
        "def f(xs):\n    for a in xs:\n        if a:\n            log(a)\n        break\n        use(a)\n",
    ),
    "Z2 js quote style change on move out of if": (
        "a.ts",
        "function f(ok) {\n  if (ok) {\n    log();\n    send('a');\n  }\n  done();\n}\n",
        'function f(ok) {\n  if (ok) {\n    log();\n  }\n  send("a");\n  done();\n}\n',
    ),
    "Z3 js out of bare block (shadowed let)": (
        "a.ts",
        "function f() {\n  let x = 1;\n  {\n    let x = 2;\n    use(x);\n  }\n  done();\n}\n",
        "function f() {\n  let x = 1;\n  {\n    let x = 2;\n  }\n  use(x);\n  done();\n}\n",
    ),
    "Z4 ts move between namespaces": (
        "a.ts",
        "namespace A {\n  init();\n  send(x);\n}\nnamespace B {\n  other();\n}\n",
        "namespace A {\n  init();\n}\nnamespace B {\n  send(x);\n  other();\n}\n",
    ),
    "Z5 py rows inserted far above + move between sibling ifs": (
        "a.py",
        "import os\n\n\n\n\n\n\ndef f(a, b, x):\n    if a:\n        send(x)\n        one()\n    if b:\n        two()\n",
        "import os\nimport sys\nimport re\n\n\n\n\n\ndef f(a, b, x):\n    if a:\n        one()\n    if b:\n        send(x)\n        two()\n",
    ),
    "Z9 js switch: break moved between cases (fallthrough)": (
        "a.ts",
        "function f(k) {\n  switch (k) {\n    case 1:\n      one();\n      break;\n    case 2:\n      two();\n  }\n}\n",
        "function f(k) {\n  switch (k) {\n    case 1:\n      one();\n    case 2:\n      two();\n      break;\n  }\n}\n",
    ),
    "Z10 py class field moved between classes": (
        "a.py",
        "class A:\n    x = 1\n    y = 2\n\n\nclass B:\n    z = 3\n",
        "class A:\n    x = 1\n\n\nclass B:\n    y = 2\n    z = 3\n",
    ),
    "Z11 py if body -> elif body": (
        "a.py",
        "def f(a, b, x):\n    if a:\n        send(x)\n        one()\n    elif b:\n        two()\n",
        "def f(a, b, x):\n    if a:\n        one()\n    elif b:\n        send(x)\n        two()\n",
    ),
    "Z12 js try body -> finally": (
        "a.ts",
        "function f(x) {\n  try {\n    send(x);\n    one();\n  } finally {\n    two();\n  }\n}\n",
        "function f(x) {\n  try {\n    one();\n  } finally {\n    send(x);\n    two();\n  }\n}\n",
    ),
    "Z12p py try body -> finally": (
        "a.py",
        "def f(x):\n    try:\n        send(x)\n        one()\n    finally:\n        two()\n",
        "def f(x):\n    try:\n        one()\n    finally:\n        send(x)\n        two()\n",
    ),
    "Z13 tsx into JSX map callback": (
        "a.tsx",
        "function F({items}) {\n  log(items);\n  return (\n    <ul>\n      {items.map((i) => {\n        mark(i);\n        return <li>{i}</li>;\n      })}\n    </ul>\n  );\n}\n",
        "function F({items}) {\n  return (\n    <ul>\n      {items.map((i) => {\n        mark(i);\n        log(items);\n        return <li>{i}</li>;\n      })}\n    </ul>\n  );\n}\n",
    ),
    "Z14 py decorator moved to another function": (
        "a.py",
        "@login_required\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n",
        "def a():\n    return 1\n\n\n@login_required\ndef b():\n    return 2\n",
    ),
    "Z15 js class field moved between classes": (
        "a.ts",
        "class A {\n  x = make();\n  y = 2;\n}\nclass B {\n  z = 3;\n}\n",
        "class A {\n  y = 2;\n}\nclass B {\n  x = make();\n  z = 3;\n}\n",
    ),
    "Z16 py with body -> outside": (
        "a.py",
        "def f(p, x):\n    with open(p) as fh:\n        read(fh)\n        send(x)\n    done()\n",
        "def f(p, x):\n    with open(p) as fh:\n        read(fh)\n    send(x)\n    done()\n",
    ),
    "Z19 js return 1 -> return 2 moved out": (
        "a.ts",
        "function f(ok) {\n  if (ok) {\n    log();\n    return 1;\n  }\n  done();\n}\n",
        "function f(ok) {\n  if (ok) {\n    log();\n  }\n  return 2;\n  done();\n}\n",
    ),
    "Z20 js continue -> break moved": (
        "a.ts",
        "function f(xs) {\n  for (const a of xs) {\n    if (a) {\n      log(a);\n      continue;\n    }\n    use(a);\n  }\n}\n",
        "function f(xs) {\n  for (const a of xs) {\n    if (a) {\n      log(a);\n    }\n    continue;\n    use(a);\n  }\n}\n",
    ),
    "C1 js leading-* ctx + quote style change, out of if": (
        "a.ts",
        "function f(ok, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n    send('a');\n  }\n  done();\n}\n",
        'function f(ok, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n  }\n  send("a");\n  done();\n}\n',
    ),
    "C2 js leading-* ctx + out of bare block": (
        "a.ts",
        "function f(width, height) {\n  let x = 1;\n  const area = width\n    * height;\n  {\n    let x = 2;\n    use(x);\n  }\n  done();\n}\n",
        "function f(width, height) {\n  let x = 1;\n  const area = width\n    * height;\n  {\n    let x = 2;\n  }\n  use(x);\n  done();\n}\n",
    ),
    "C3 js leading-* ctx + return 'a' -> return 'b' out of if": (
        "a.ts",
        'function f(ok, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n    return "a";\n  }\n  done();\n}\n',
        'function f(ok, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n  }\n  return "b";\n  done();\n}\n',
    ),
    "C4 py docstring hunk-start + return 'a'->'b' between sibling ifs": (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        return \'a\'\n        one()\n    if b:\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        return \'b\'\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
    ),
    "C5 py docstring hunk-start + quote change between sibling ifs": (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        send(\'a\')\n        one()\n    if b:\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        send("a")\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
    ),
    "C6 js template both-ends + quote change": (
        "a.ts",
        "const css = `\n  .a { color: red; }\n  .b {\n  }\n`;\nif (ok) {\n  send('a');\n}\ndone();\nconst sql = `\n  SELECT\n  FROM t\n  WHERE\n`;\n",
        'const css = `\n  .a { color: red; }\n  .b {\n  }\n`;\nif (ok) {\n}\nsend("a");\nconst sql = `\n  SELECT\n  FROM t\n  WHERE\n`;\n',
    ),
    "E1 unclosed brace far below, both sides": (
        "a.ts",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n  }\n  done();\n}\nfunction h0() {\n  w0();\n}\nfunction h1() {\n  w1();\n}\nfunction h2() {\n  w2();\n}\nfunction h3() {\n  w3();\n}\nfunction h4() {\n  w4();\n}\nfunction h5() {\n  w5();\n}\nfunction h6() {\n  w6();\n}\nfunction h7() {\n  w7();\n}\nfunction g() {\n  if (y) {\n    z();\n",
        "function f(ok, x) {\n  if (ok) {\n    keep();\n  }\n  send(x);\n  done();\n}\nfunction h0() {\n  w0();\n}\nfunction h1() {\n  w1();\n}\nfunction h2() {\n  w2();\n}\nfunction h3() {\n  w3();\n}\nfunction h4() {\n  w4();\n}\nfunction h5() {\n  w5();\n}\nfunction h6() {\n  w6();\n}\nfunction h7() {\n  w7();\n}\nfunction g() {\n  if (y) {\n    z();\n",
    ),
    "E2 garbage far above, both sides": (
        "a.ts",
        "const q = (;\nfunction h0() {\n  w0();\n}\nfunction h1() {\n  w1();\n}\nfunction h2() {\n  w2();\n}\nfunction h3() {\n  w3();\n}\nfunction h4() {\n  w4();\n}\nfunction h5() {\n  w5();\n}\nfunction h6() {\n  w6();\n}\nfunction h7() {\n  w7();\n}\nfunction f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n  }\n  done();\n}\n",
        "const q = (;\nfunction h0() {\n  w0();\n}\nfunction h1() {\n  w1();\n}\nfunction h2() {\n  w2();\n}\nfunction h3() {\n  w3();\n}\nfunction h4() {\n  w4();\n}\nfunction h5() {\n  w5();\n}\nfunction h6() {\n  w6();\n}\nfunction h7() {\n  w7();\n}\nfunction f(ok, x) {\n  if (ok) {\n    keep();\n  }\n  send(x);\n  done();\n}\n",
    ),
    "E3 unclosed brace far above, both sides": (
        "a.ts",
        "function g() {\n  if (y) {\nfunction h0() {\n  w0();\n}\nfunction h1() {\n  w1();\n}\nfunction h2() {\n  w2();\n}\nfunction h3() {\n  w3();\n}\nfunction h4() {\n  w4();\n}\nfunction h5() {\n  w5();\n}\nfunction h6() {\n  w6();\n}\nfunction h7() {\n  w7();\n}\nfunction f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n  }\n  done();\n}\n",
        "function g() {\n  if (y) {\nfunction h0() {\n  w0();\n}\nfunction h1() {\n  w1();\n}\nfunction h2() {\n  w2();\n}\nfunction h3() {\n  w3();\n}\nfunction h4() {\n  w4();\n}\nfunction h5() {\n  w5();\n}\nfunction h6() {\n  w6();\n}\nfunction h7() {\n  w7();\n}\nfunction f(ok, x) {\n  if (ok) {\n    keep();\n  }\n  send(x);\n  done();\n}\n",
    ),
    "E4 py unclosed paren far above": (
        "a.py",
        "x = (\ndef h0():\n    w0()\ndef h1():\n    w1()\ndef h2():\n    w2()\ndef h3():\n    w3()\ndef h4():\n    w4()\ndef h5():\n    w5()\ndef h6():\n    w6()\ndef h7():\n    w7()\ndef f(ok, x):\n    if ok:\n        keep()\n        send(x)\n    done()\n",
        "x = (\ndef h0():\n    w0()\ndef h1():\n    w1()\ndef h2():\n    w2()\ndef h3():\n    w3()\ndef h4():\n    w4()\ndef h5():\n    w5()\ndef h6():\n    w6()\ndef h7():\n    w7()\ndef f(ok, x):\n    if ok:\n        keep()\n    send(x)\n    done()\n",
    ),
    "E5 py unclosed paren far below": (
        "a.py",
        "def f(ok, x):\n    if ok:\n        keep()\n        send(x)\n    done()\ndef h0():\n    w0()\ndef h1():\n    w1()\ndef h2():\n    w2()\ndef h3():\n    w3()\ndef h4():\n    w4()\ndef h5():\n    w5()\ndef h6():\n    w6()\ndef h7():\n    w7()\nx = (\n",
        "def f(ok, x):\n    if ok:\n        keep()\n    send(x)\n    done()\ndef h0():\n    w0()\ndef h1():\n    w1()\ndef h2():\n    w2()\ndef h3():\n    w3()\ndef h4():\n    w4()\ndef h5():\n    w5()\ndef h6():\n    w6()\ndef h7():\n    w7()\nx = (\n",
    ),
    "NOCTX C1 js leading-* ctx + quote style change, out of if": (
        "a.ts",
        "function f(ok, width, height) {\n  const area = width * height;\n  if (ok) {\n    keep();\n    send('a');\n  }\n  done();\n}\n",
        'function f(ok, width, height) {\n  const area = width * height;\n  if (ok) {\n    keep();\n  }\n  send("a");\n  done();\n}\n',
    ),
    "NOCTX C2 js leading-* ctx + out of bare block": (
        "a.ts",
        "function f(width, height) {\n  let x = 1;\n  const area = width * height;\n  {\n    let x = 2;\n    use(x);\n  }\n  done();\n}\n",
        "function f(width, height) {\n  let x = 1;\n  const area = width * height;\n  {\n    let x = 2;\n  }\n  use(x);\n  done();\n}\n",
    ),
    "NOCTX C3 js leading-* ctx + return 'a' -> return 'b' out of if": (
        "a.ts",
        'function f(ok, width, height) {\n  const area = width * height;\n  if (ok) {\n    keep();\n    return "a";\n  }\n  done();\n}\n',
        'function f(ok, width, height) {\n  const area = width * height;\n  if (ok) {\n    keep();\n  }\n  return "b";\n  done();\n}\n',
    ),
    "NOCTX C4 py docstring hunk-start + return 'a'->'b' between sibling ifs": (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        return \'a\'\n        one()\n    if b:\n        two()\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        return \'b\'\n        two()\n',
    ),
    "NOCTX C5 py docstring hunk-start + quote change between sibling ifs": (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        send(\'a\')\n        one()\n    if b:\n        two()\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        send("a")\n        two()\n',
    ),
    "D1 js bare block move only": (
        "a.ts",
        "function f() {\n  let x = 1;\n  {\n    let x = 2;\n    use(x);\n  }\n  done();\n}\n",
        "function f() {\n  let x = 1;\n  {\n    let x = 2;\n  }\n  use(x);\n  done();\n}\n",
    ),
    "D2 js try-block bare move with far edit": (
        "a.ts",
        "function f() {\n  let x = 1;\n  {\n    let x = 2;\n    a();\n    b();\n    use(x);\n  }\n  done();\n}\n",
        "function f() {\n  let x = 1;\n  {\n    let x = 2;\n    a();\n    b();\n  }\n  use(x);\n  done();\n}\n",
    ),
    "F1 py f-string {x} quote change between sibling ifs (blinded)": (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        send(f\'{x}\')\n        one()\n    if b:\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        send(f"{x}")\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
    ),
    "F1b py f-string {x} same quotes between sibling ifs (blinded)": (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        send(f\'{x}\')\n        one()\n    if b:\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        send(f\'{x}\')\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
    ),
    "F1c js template ${x} -> string concat out of if (blinded)": (
        "a.ts",
        "function f(ok, x, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n    send(`a${x}`);\n  }\n  done();\n}\n",
        "function f(ok, x, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n  }\n  send(`a${ x }`);\n  done();\n}\n",
    ),
    "F2 ts method decorator moved a()->b()": (
        "a.ts",
        "class S {\n  @Get()\n  a() {\n    return 1;\n  }\n\n  b() {\n    return 2;\n  }\n}\n",
        "class S {\n  a() {\n    return 1;\n  }\n\n  @Get()\n  b() {\n    return 2;\n  }\n}\n",
    ),
    "F3 ts class decorator removed": (
        "a.ts",
        "@Injectable()\nclass S {\n  a() {\n    return 1;\n  }\n}\n",
        "class S {\n  a() {\n    return 1;\n  }\n}\n",
    ),
    "F4 ts stacked method decorators reordered": (
        "a.ts",
        "class S {\n  @A()\n  @B()\n  a() {\n    return 1;\n  }\n}\n",
        "class S {\n  @B()\n  @A()\n  a() {\n    return 1;\n  }\n}\n",
    ),
    "F5 ts param decorator moved between params": (
        "a.ts",
        "class S {\n  constructor(\n    @Inject(A) private x: X,\n    private y: Y,\n  ) {}\n}\n",
        "class S {\n  constructor(\n    private x: X,\n    @Inject(A) private y: Y,\n  ) {}\n}\n",
    ),
    "F6b py stacked decorators reordered": (
        "a.py",
        "@a\n@b\ndef f():\n    return 1\n",
        "@b\n@a\ndef f():\n    return 1\n",
    ),
    "F6c py decorator with args moved a->b": (
        "a.py",
        "@limit(5, key='u')\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n",
        "def a():\n    return 1\n\n\n@limit(5, key='u')\ndef b():\n    return 2\n",
    ),
    "F7 js labeled block move out (blinded)": (
        "a.ts",
        "function f(width, height) {\n  let x = 1;\n  const area = width\n    * height;\n  outer: {\n    let x = 2;\n    use(x);\n  }\n  done();\n}\n",
        "function f(width, height) {\n  let x = 1;\n  const area = width\n    * height;\n  outer: {\n    let x = 2;\n  }\n  use(x);\n  done();\n}\n",
    ),
    "F8 js class static block move out to field init (blinded)": (
        "a.ts",
        "class S {\n  static w = width\n    * height;\n  static {\n    keep();\n    init();\n  }\n  m() {}\n}\n",
        "class S {\n  static w = width\n    * height;\n  static {\n    keep();\n  }\n  m() {}\n}\ninit();\n",
    ),
    "F9 js 'a' -> `a` out of if (blinded)": (
        "a.ts",
        "function f(ok, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n    send('a');\n  }\n  done();\n}\n",
        "function f(ok, width, height) {\n  const area = width\n    * height;\n  if (ok) {\n    keep();\n  }\n  send(`a`);\n  done();\n}\n",
    ),
    "F10 py b'a' -> 'a' between sibling ifs (blinded)": (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        send(b\'a\')\n        one()\n    if b:\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        send(\'a\')\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
    ),
    "F11 js if-branch swap bare block in switch case (blinded)": (
        "a.ts",
        "function f(k, width, height) {\n  const area = width\n    * height;\n  switch (k) {\n    case 1: {\n      let x = 2;\n      use(x);\n    }\n    case 2:\n      done();\n  }\n}\n",
        "function f(k, width, height) {\n  const area = width\n    * height;\n  switch (k) {\n    case 1: {\n      let x = 2;\n    }\n      use(x);\n    case 2:\n      done();\n  }\n}\n",
    ),
    "F12 py raw/escaped quote edit inside string, out of if (blinded)": (
        "a.py",
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        send(\'it\\\'s\')\n        one()\n    if b:\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
        'def f(a, b, x):\n    """Doc.\n\n    Example: if a: run()\n    Ends here."""\n    if a:\n        one()\n    if b:\n        send("it\'s")\n        two()\n    def g():\n        """Second doc.\n\n        More.\n        """\n',
    ),
    "W1b CRLF move out of if (expect held)": (
        "a.py",
        "def f(ok, x):\r\n    if ok:\r\n        keep()\r\n        send(x)\r\n    done()\r\n",
        "def f(ok, x):\r\n    if ok:\r\n        keep()\r\n    send(x)\r\n    done()\r\n",
    ),
    "W1f form feed + move out of if (expect held)": (
        "a.py",
        "x = 1\n\x0c\ndef f(ok, x):\n    if ok:\n        keep()\n        send(x)\n    done()\n",
        "x = 1\n\x0c\ndef f(ok, x):\n    if ok:\n        keep()\n    send(x)\n    done()\n",
    ),
    "G1 ts param decorator moved x->y, param a removed + z added (rows pair": (
        "a.ts",
        "class S {\n  constructor(\n    a: A,\n    @Inject(T) x: X,\n    y: Y,\n  ) {}\n}\n",
        "class S {\n  constructor(\n    x: X,\n    @Inject(T) y: Y,\n    z: Z,\n  ) {}\n}\n",
    ),
    "G1b ts param decorator moved x->y one row, a removed": (
        "a.ts",
        "class S {\n  m(a: A, @Inject(T) x: X, y: Y) {\n    return 1;\n  }\n}\n",
        "class S {\n  m(x: X, @Inject(T) y: Y) {\n    return 1;\n  }\n}\n",
    ),
    "G2 ts stacked reorder + rename one": (
        "a.ts",
        "class S {\n  @A()\n  @B()\n  m() {\n    return 1;\n  }\n}\n",
        "class S {\n  @B()\n  @A2()\n  m() {\n    return 1;\n  }\n}\n",
    ),
    "G2b py stacked reorder + rename one": (
        "a.py",
        "@a\n@b\ndef f():\n    return 1\n",
        "@b\n@a2\ndef f():\n    return 1\n",
    ),
    "G3 ts decorator moved a->b with both methods renamed": (
        "a.ts",
        "class S {\n  @Get()\n  a() {\n    return 1;\n  }\n\n  b() {\n    return 2;\n  }\n}\n",
        "class S {\n  a2() {\n    return 1;\n  }\n\n  @Get()\n  b2() {\n    return 2;\n  }\n}\n",
    ),
    "G3b ts decorator moved a->b, swapping method order": (
        "a.ts",
        "class S {\n  @Get()\n  a() {\n    return 1;\n  }\n  b() {\n    return 2;\n  }\n}\n",
        "class S {\n  @Get()\n  b() {\n    return 2;\n  }\n  a() {\n    return 1;\n  }\n}\n",
    ),
    "G3c ts decorator moved from field to field": (
        "a.ts",
        "class S {\n  @Input() x = 1;\n  y = 2;\n}\n",
        "class S {\n  x = 1;\n  @Input() y = 2;\n}\n",
    ),
    "G3d ts decorator stays, method swapped above (expect held? decorator f": (
        "a.ts",
        "class S {\n  b() {\n    return 2;\n  }\n  @Get()\n  a() {\n    return 1;\n  }\n}\n",
        "class S {\n  @Get()\n  a() {\n    return 1;\n  }\n  b() {\n    return 2;\n  }\n}\n",
    ),
    "G4 py decorator moved to the other def (one-line defs, paired)": (
        "a.py",
        "@cache\ndef a(): return 1\ndef b(): return 2\n",
        "def a(): return 1\n@cache\ndef b(): return 2\n",
    ),
}

OFFERED = {
    "T7 js await added on move out of if (known limit)": (
        "a.ts",
        "async function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n  }\n  done();\n}\n",
        "async function f(ok, x) {\n  if (ok) {\n    keep();\n  }\n  await send(x);\n  done();\n}\n",
    ),
    "T15 py if-move, kwarg added (shape change)": (
        "a.py",
        "def f(ok, x):\n    if ok:\n        keep()\n        send(x)\n    done()\n",
        "def f(ok, x):\n    if ok:\n        keep()\n    send(x, retry=True)\n    done()\n",
    ),
    "V8 js guard clause inserted (contained run) before stmt": (
        "a.ts",
        "function f(ok, x) {\n  prep();\n  send(x);\n}\n",
        "function f(ok, x) {\n  prep();\n  if (!ok) return;\n  send(x);\n}\n",
    ),
    "Z17 js expression-bodied arrow: stmt into callback by rewrite": (
        "a.ts",
        "function f(xs, x) {\n  xs.forEach((a) => keep(a));\n  send(x);\n}\n",
        "function f(xs, x) {\n  xs.forEach((a) => (keep(a), send(x)));\n}\n",
    ),
    "Z18 js ternary-as-statement: move out": (
        "a.ts",
        "function f(ok, x) {\n  if (ok) {\n    log();\n    send(x);\n  }\n}\n",
        "function f(ok, x) {\n  if (ok) {\n    log();\n  }\n  ok ? send(x) : null;\n}\n",
    ),
    "F3b ts method decorator removed": (
        "a.ts",
        "class S {\n  @Get()\n  a() {\n    return 1;\n  }\n\n  b() {\n    return 2;\n  }\n}\n",
        "class S {\n  a() {\n    return 1;\n  }\n\n  b() {\n    return 2;\n  }\n}\n",
    ),
    "F6 py decorator removed": (
        "a.py",
        "@login_required\ndef a():\n    return 1\n",
        "def a():\n    return 1\n",
    ),
    "G5 js 'a' vs tagged template tag`a` out of if": (
        "a.ts",
        "function f(ok) {\n  if (ok) {\n    keep();\n    send('a');\n  }\n  done();\n}\n",
        "function f(ok) {\n  if (ok) {\n    keep();\n  }\n  send(tag`a`);\n  done();\n}\n",
    ),
}

FREE = {
    "W1a CRLF free reorder (expect offered)": (
        "a.py",
        "def f(a, b):\r\n    one(a)\r\n    two(b)\r\n    return a\r\n",
        "def f(a, b):\r\n    two(b)\r\n    one(a)\r\n    return a\r\n",
    ),
    "W1c no EOL at EOF, free reorder (expect offered)": (
        "a.py",
        "def f(a, b):\n    one(a)\n    two(b)\n    return a",
        "def f(a, b):\n    two(b)\n    one(a)\n    return a",
    ),
    "W1d no EOL, last line edited (expect offered)": (
        "a.py",
        "def f(a, b):\n    one(a)\n    two(b)\n    return a",
        "def f(a, b):\n    one(a)\n    two(b)\n    return b",
    ),
    "W1e form feed line in file, free reorder (expect offered)": (
        "a.py",
        "x = 1\n\x0c\ndef f(a, b):\n    one(a)\n    two(b)\n    return a\n",
        "x = 1\n\x0c\ndef f(a, b):\n    two(b)\n    one(a)\n    return a\n",
    ),
    "W1g tab-indented free reorder (expect offered)": (
        "a.py",
        "def f(a, b):\n\tone(a)\n\ttwo(b)\n\treturn a\n",
        "def f(a, b):\n\ttwo(b)\n\tone(a)\n\treturn a\n",
    ),
    "W1h U+2028 in js string, free reorder (expect offered)": (
        "a.ts",
        "const s = 'a\u2028b';\nfunction f() {\n  one();\n  two();\n}\n",
        "const s = 'a\u2028b';\nfunction f() {\n  two();\n  one();\n}\n",
    ),
    "W1i lone CR in a line, free reorder (expect offered)": (
        "a.py",
        "x = 'a\rb'\ndef f(a, b):\n    one(a)\n    two(b)\n    return a\n",
        "x = 'a\rb'\ndef f(a, b):\n    two(b)\n    one(a)\n    return a\n",
    ),
    "G1c ts param decorator stays on x, param added before (expect free)": (
        "a.ts",
        "class S {\n  constructor(\n    @Inject(T) x: X,\n  ) {}\n}\n",
        "class S {\n  constructor(\n    w: W,\n    @Inject(T) x: X,\n  ) {}\n}\n",
    ),
}
