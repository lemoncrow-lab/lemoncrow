<img alt="P0" src="https://lemoncrow.com/badges/p0.svg?v=1" align="top"> **Token is logged in clear text**

The changed branch returns before it rejects a missing principal.

**Consequence:** An unauthenticated request can reach behaviour meant for administrators.

```suggestion
    logger.info('login ok')
```

---

<details><summary>Prompt To Fix With AI</summary>

````text
This is a comment left during a code review.
Path: src/auth.py
Line: 31

Comment:
**Token is logged in clear text**
The changed branch returns before it rejects a missing principal.

**Consequence:** An unauthenticated request can reach behaviour meant for administrators.

Suggested replacement for line 31, checked against this commit:
```
    logger.info('login ok')
```

Verify that the problem is real in the current code, then fix it in the smallest way that keeps the rest of the behaviour. If the comment is wrong, say why and change nothing.
````

</details>

**Rule used:** `no-secrets-in-logs`

<sub>Must fix (P0) · LemonCrow AI review</sub>
<!-- lemoncrow:fingerprint fp-0123456789abcdef -->
<!-- lemoncrow:finding p0 -->