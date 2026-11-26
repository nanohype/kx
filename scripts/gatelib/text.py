"""Reading source text the way a gate needs to."""


def strip_comments(text: str) -> str:
    """Source with `#` comment bodies blanked, quote-aware.

    A `#` opens a comment only at the start of a word and only outside quotes,
    so a URL fragment and a quoted hash both survive. Bodies are blanked rather
    than dropped, so every line keeps its length and a file:line citation made
    from the blanked view stays true of the file.

    Which view a check reads is a per-check decision, not a blanket rule:

      raw       when the thing being looked for IS an annotation, such as a lint
                suppression or an update-tool directive. Annotations are
                comments by construction, so stripping blinds the check to them.
      stripped  when a comment must not be able to satisfy a code reference — a
                pin, a call, a filename something applies.
    """
    out = []
    for line in text.splitlines():
        # An unbalanced quote must not swallow the rest of the line. A word like
        # dont'care opens a quote that never closes, and every `#` after it then
        # reads as quoted — so a comment survives blanking and whatever it
        # mentions counts as code. Scanned twice: if a quote is still open at
        # end of line it was an apostrophe inside a word, so the second pass
        # treats that character as literal.
        buf = []
        for literal_apostrophe in (False, True):
            buf, quote, i = [], None, 0
            while i < len(line):
                c = line[i]
                if quote:
                    buf.append(c)
                    if c == "\\" and quote == '"' and i + 1 < len(line):
                        buf.append(line[i + 1])
                        i += 2
                        continue
                    if c == quote:
                        quote = None
                elif c == "'" and literal_apostrophe:
                    buf.append(c)
                elif c in "\"'":
                    quote = c
                    buf.append(c)
                elif c == "#" and (not buf or buf[-1].isspace()):
                    buf.append(" " * (len(line) - i))
                    quote = None
                    break
                else:
                    buf.append(c)
                i += 1
            if quote is None:
                break
        out.append("".join(buf))
    return "\n".join(out)
