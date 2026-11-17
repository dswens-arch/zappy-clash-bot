"""
zappy_news_sparks.py
--------------------
Where Spark art lives, so Agent Wire cards about the Office can show the Sparks
themselves. Same Pinata gateway + CIDs that /sparkstatus in spark_admin.py uses
for its thumbnail (copied here because the T2/T3 maps are local to that command).
Override the gateway with SPARK_IMAGE_GATEWAY if you ever move it.
"""

import os

GATEWAY = os.environ.get("SPARK_IMAGE_GATEWAY", "https://scarlet-written-scallop-153.mypinata.cloud/ipfs/")

# tier -> spark_type -> image CID
CIDS = {
    1: {
        "zolt": "bafkreign5ydt5zj4mltsays47lsp7d6stdzje756zvzehzx7tryqxmqv6q",
        "scorch": "bafkreihpdkwbg6gvn4zutkohrmig67zalqtprg4wln624zkukw5nlo4f3q",
        "jinx": "bafkreierm7ti75i2hdpngztupgqpr5w7lbjull262outivdaanz4lshd7u",
        "moss": "bafkreib2iprbja2zhfdxflulcgqyav2m5i4z5behc3n6kt4qglus36dqqm",
        "glitch": "bafkreidtjpiu4k44u5soltmqsfo6oyykef2p7mytw4jte6tn47dedrwhye",
        "null": "bafkreif7otb4ifgcgkqqsvxu3jgxe62yrvdwrgnbe37b5vwr4l5ypnmnpe",
    },
    2: {
        "zolt": "bafybeia37zmaybuc6tiwy2ji22ub7fmuub6khdkjdl65s34inp5lzzwxae",
        "scorch": "bafybeieksknm2nt4akeiht6ezu3jnv3bkv3gvh5p42mrvinivldrkh663m",
        "jinx": "bafybeicixawcaxwmzlegcylavymtv3flxanpoo35gljwbbqbt3jfx4ywtm",
        "moss": "bafybeifqz2ffykrpsjxmt4ktp7zzob3nkxdeaxh5ht7l62ysoyvwux6wfm",
        "glitch": "bafybeiayhxvs72ceoygrpuirwuworkbrvuhdeuvqi3cvhn5grbc44k6lje",
        "null": "bafkreiayd2s5tw3eo676ofwuw47p5lcslsisbjdh4bsgm5krokw6a4uwoy",
    },
    3: {
        "zolt": "bafybeigephla6nmi65gn46stp7dbz72p5or5rfeww4tvdpp2sv2cf5b3ou",
        "scorch": "bafybeiemybyw7g3h655mf6ikqdnvse6cx3uze7stkqzsmyqhh42v6lqjoa",
        "jinx": "bafybeibziy5smed5hbfphrwoha4w2nbytrzulxvqgfrp3jyvblpmi2ng3i",
        "moss": "bafybeicdmpnisqaldipjyfhxqukpk6xeo6rvoknomfvkxpeec63edpadnq",
        "glitch": "bafybeid4s6immn5o7sl62eyqydfsq4cyou3kxv6i42szz4ryjqtxhwwhzi",
        "null": "bafybeihtecxwqvlknjwwtcq42emldzm6s3ohkof6vcziikzsyr5m62jjte",
    },
}

# Same accent colours /sparkstatus uses, as RGB.
COLORS = {
    "zolt": (0xc8, 0xff, 0x00), "scorch": (0xff, 0x5a, 0x1f), "jinx": (0xa7, 0x8b, 0xfa),
    "moss": (0x3d, 0xff, 0x9a), "glitch": (0xff, 0x2d, 0x78), "null": (0x94, 0xa3, 0xb8),
}
DEFAULT_COLOR = (0x60, 0xa5, 0xfa)


def spark_image_url(spark_type: str | None, tier: int | None) -> str | None:
    cid = CIDS.get(int(tier or 1), CIDS[1]).get((spark_type or "").lower())
    return f"{GATEWAY}{cid}" if cid else None


def spark_color(spark_type: str | None) -> tuple:
    return COLORS.get((spark_type or "").lower(), DEFAULT_COLOR)
