import re

STOPWORDS = {
    "a", "al", "als", "amb", "d", "de", "del", "dels", "e", "el", "els", "en", "i", "l",
    "la", "las", "les", "los", "o", "para", "per", "pel", "pels", "y",
}
ROMAN = re.compile(r"^(?=[IVXLC]+$)C{0,3}(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def abbreviate(name: str) -> str:
    """Short label for a course: "Itinerari personal per a l'ocupabilitat I" -> "IPO I"."""
    name = re.sub(r"\([^)]*\)", " ", name)  # drop "(GS)" and similar
    letters, suffix = "", []
    for word in re.split(r"[\s/,:-]+", name):
        word = re.split(r"[’']", word)[-1].strip(".")  # l'ocupabilitat -> ocupabilitat
        if ROMAN.match(word) or word.isdigit():  # checked first: "I" is a numeral, "i" means "and"
            suffix.append(word)
        elif not word or word.lower() in STOPWORDS:
            continue
        elif word.isupper() and len(word) > 1:
            letters += word  # keep existing acronyms (FP, ISO)
        else:
            letters += word[0].upper()
    return " ".join([letters, *suffix]).strip() or name.strip()


def course_labels(courses: list[dict]) -> dict[int, str]:
    """Course id -> label, falling back to the short name when abbreviations clash."""
    abbr = {c["id"]: abbreviate(c["fullname"]) for c in courses}
    counts: dict[str, int] = {}
    for a in abbr.values():
        counts[a] = counts.get(a, 0) + 1
    return {c["id"]: abbr[c["id"]] if counts[abbr[c["id"]]] == 1 else f"{abbr[c['id']]} ({c['shortname']})"
            for c in courses}
