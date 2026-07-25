"""RTH Opportunity Scanner — controlled Discovery Universe.

Curated liquid US names: S&P 500 majors + Nasdaq-100 + sector/major
ETFs + approved liquid-growth extension (~420 symbols). Operator can
extend/exclude via runtime_flags scanner_policy
(extra_symbols / exclude_symbols) without redeploying.

Leveraged/inverse ETFs are hard-excluded by default (policy
`allow_leveraged=true` to override) per operator doctrine.
"""
from __future__ import annotations

ETFS = [
    "SPY", "QQQ", "IWM", "DIA", "MDY", "RSP", "VTI", "VOO",
    "XLF", "XLK", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "SMH", "XBI", "IBB", "KRE", "XOP", "OIH", "GDX", "GDXJ", "XME", "ITB", "XHB",
    "ARKK", "KWEB", "FXI", "EEM", "EFA", "EWZ", "EWJ", "INDA",
    "GLD", "SLV", "USO", "UNG", "TLT", "IEF", "HYG", "LQD",
    "IBIT", "FBTC", "ETHA",
]

LEVERAGED_INVERSE = {
    "TQQQ", "SQQQ", "SPXU", "SPXL", "UPRO", "SDOW", "UDOW", "QLD", "QID",
    "SSO", "SDS", "TNA", "TZA", "SOXL", "SOXS", "LABU", "LABD", "UVXY",
    "SVXY", "VXX", "VIXY", "TSLL", "TSLQ", "NVDL", "NVDQ", "YINN", "YANG",
    "FAS", "FAZ", "ERX", "ERY", "DRN", "DRV", "TMF", "TMV", "BOIL", "KOLD",
    "UCO", "SCO", "AGQ", "ZSL", "NUGT", "DUST", "JNUG", "JDST", "WEBL",
    "WEBS", "TECL", "TECS", "CURE", "DPST", "MSTU", "MSTZ", "CONL", "ETHU",
    "BITX", "SPXS", "UMDD", "SMDD", "TVIX",
}

SP500_NDX_CORE = [
    # Mega/large tech + NDX
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO",
    "ORCL", "CRM", "ADBE", "AMD", "INTC", "QCOM", "TXN", "AMAT", "LRCX",
    "KLAC", "MU", "ADI", "NXPI", "MRVL", "ON", "MCHP", "SWKS", "TER",
    "NFLX", "CSCO", "IBM", "NOW", "INTU", "PANW", "CRWD", "FTNT", "ZS",
    "DDOG", "SNOW", "NET", "MDB", "TEAM", "WDAY", "ADSK", "SNPS", "CDNS",
    "ANSS", "PLTR", "APP", "SMCI", "DELL", "HPQ", "HPE", "WDC", "STX",
    "ANET", "CIEN", "JNPR", "ZM", "DOCU", "OKTA", "TWLO", "SHOP", "SQ",
    "PYPL", "COIN", "HOOD", "MSTR", "SOFI", "AFRM", "UPST", "RBLX", "U",
    "ABNB", "UBER", "LYFT", "DASH", "EXPE", "BKNG", "EA", "TTWO", "ROKU",
    "SPOT", "PINS", "SNAP", "TTD", "GTLB", "IOT", "PATH", "AI", "IONQ",
    "RGTI", "ARM", "TSM", "ASML", "BABA", "JD", "PDD", "NIO", "XPEV", "LI",
    "RIVN", "LCID", "GM", "F", "TM", "STLA",
    # Financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "BX", "KKR",
    "APO", "AXP", "V", "MA", "COF", "DFS", "USB", "PNC", "TFC", "BK",
    "STT", "CME", "ICE", "NDAQ", "SPGI", "MCO", "MSCI", "AIG", "MET",
    "PRU", "ALL", "TRV", "PGR", "CB", "AFL", "HIG",
    # Health
    "UNH", "LLY", "JNJ", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR", "BMY",
    "AMGN", "GILD", "VRTX", "REGN", "BIIB", "MRNA", "ISRG", "SYK", "BSX",
    "MDT", "EW", "ZBH", "BDX", "CI", "CVS", "HUM", "ELV", "MCK", "CAH",
    "HCA", "DXCM", "IDXX", "IQV", "A", "RMD", "WAT",
    # Consumer
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "LULU", "SBUX", "MCD", "CMG",
    "YUM", "DPZ", "DRI", "KO", "PEP", "MDLZ", "MNST", "KDP", "STZ", "PG",
    "CL", "KMB", "EL", "KHC", "GIS", "K", "HSY", "SYY", "KR", "DG", "DLTR",
    "ROST", "TJX", "BBY", "ULTA", "ORLY", "AZO", "AAP", "GPC", "EBAY",
    "ETSY", "W", "CHWY", "CVNA", "KMX", "MAR", "HLT", "RCL", "CCL", "NCLH",
    "LVS", "WYNN", "MGM", "DKNG", "PENN", "CZR",
    # Industrials / energy / materials
    "BA", "CAT", "DE", "GE", "GEV", "HON", "MMM", "RTX", "LMT", "NOC",
    "GD", "LHX", "TDG", "HWM", "ETN", "EMR", "ITW", "PH", "ROK", "DOV",
    "CMI", "PCAR", "URI", "PWR", "FDX", "UPS", "UNP", "CSX", "NSC", "DAL",
    "UAL", "AAL", "LUV", "XOM", "CVX", "COP", "EOG", "SLB", "HAL", "BKR",
    "OXY", "PSX", "VLO", "MPC", "PXD", "DVN", "FANG", "HES", "WMB", "KMI",
    "OKE", "ET", "LNG", "FCX", "NEM", "NUE", "STLD", "CLF", "AA", "X",
    "DOW", "LYB", "DD", "APD", "LIN", "ECL", "SHW", "VMC", "MLM", "ALB",
    "MP",
    # Utilities / REITs / telecom / media
    "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "ED", "PCG",
    "VST", "CEG", "NRG", "PLD", "AMT", "CCI", "EQIX", "DLR", "O", "SPG",
    "WELL", "AVB", "T", "VZ", "TMUS", "CMCSA", "CHTR", "DIS", "WBD",
    "PARA", "FOXA", "LYV",
    # Liquid growth extension
    "CELH", "ELF", "DUOL", "TOST", "CAVA", "WING", "ONON", "DECK", "CROX",
    "ANF", "GAP", "AEO", "VRT", "MOD", "NVT", "BE", "PLUG", "FSLR", "ENPH",
    "SEDG", "RUN", "NEE", "RKLB", "ASTS", "LUNR", "ACHR", "JOBY", "OKLO",
    "SMR", "CCJ", "UEC", "TLN", "GME", "AMC",
]


def discovery_universe(policy: dict | None = None) -> list[str]:
    """Deduped, policy-adjusted discovery universe."""
    policy = policy or {}
    extra = [str(s).upper().strip() for s in (policy.get("extra_symbols") or [])]
    exclude = {str(s).upper().strip() for s in (policy.get("exclude_symbols") or [])}
    allow_lev = bool(policy.get("allow_leveraged"))
    out: list[str] = []
    for sym in [*ETFS, *SP500_NDX_CORE, *extra]:
        if sym in exclude or sym in out:
            continue
        if not allow_lev and sym in LEVERAGED_INVERSE:
            continue
        out.append(sym)
    return out
