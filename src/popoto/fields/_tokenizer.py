"""Shared tokenization logic for text-indexing fields.

Used by ExistenceFilter (Bloom filter) and BM25Field (ranked keyword search).
Extracts the tokenization logic into a shared module so both fields use
identical preprocessing: lowercase, split on non-word characters, filter
short tokens, and remove common English stop words.
"""

import re

# Minimum token length to include. Tokens shorter than this are noise.
MIN_TOKEN_LENGTH = 3

# Common English stop words filtered out during tokenization.
STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "can",
        "had",
        "her",
        "was",
        "one",
        "our",
        "out",
        "has",
        "his",
        "how",
        "its",
        "may",
        "new",
        "now",
        "old",
        "see",
        "way",
        "who",
        "did",
        "got",
        "let",
        "say",
        "she",
        "too",
        "use",
        "with",
        "have",
        "from",
        "this",
        "that",
        "they",
        "been",
        "will",
        "into",
        "than",
        "them",
        "then",
        "what",
        "when",
        "which",
        "would",
        "there",
        "their",
        "about",
    }
)

# Split pattern: any sequence of non-word characters
_SPLIT_PATTERN = re.compile(r"\W+")


def tokenize(text, unique=True):
    """Tokenize a text string into individual terms for indexing.

    Lowercases the input, splits on non-word characters, filters out tokens
    shorter than 3 characters and common English stop words.

    Args:
        text: The text string to tokenize.
        unique: If True (default), deduplicate tokens. Set to False to
            preserve raw term counts needed for BM25 term frequency.

    Returns:
        list[str]: Tokens suitable for indexing. Deduplicated if unique=True,
            raw (with repeats) if unique=False.
            Returns an empty list if no tokens survive filtering.
    """
    if not text:
        return []
    lowered = text.lower()
    raw_tokens = _SPLIT_PATTERN.split(lowered)
    if unique:
        seen = set()
        tokens = []
        for t in raw_tokens:
            if len(t) >= MIN_TOKEN_LENGTH and t not in STOP_WORDS and t not in seen:
                seen.add(t)
                tokens.append(t)
        return tokens
    else:
        return [
            t
            for t in raw_tokens
            if len(t) >= MIN_TOKEN_LENGTH and t not in STOP_WORDS
        ]
