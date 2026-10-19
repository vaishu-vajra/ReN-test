from collections import defaultdict
SECTION_PRIORITY = {
    "overview and key-metrics": ["DEI Report", "Sustainability Report", "Annual Report"],
    # "key-metrics": ["DEI Report", "Sustainability Report", "Annual Report"],
    "key-personal": ["DEI Report", "Sustainability Report", "Annual Report"],
    "key-initiatives": ["DEI Report", "Sustainability Report", "Annual Report"],
    "key-priorities": ["DEI Report", "Sustainability Report", "Annual Report"],
    "dimensions-summary": ["DEI Report", "Sustainability Report", "Annual Report"],
    "board-members": ["Annual Report"],
    "environmental": [
        "Sustainability Report",
        "Annual Report",
        # "10-K",
        # "20-F"
    ],
    "governance": ["Annual Report", "Sustainability Report"],
    "social": ["Annual Report", "DEI Report", "Sustainability Report"],
}


def pick_best_docs_by_year(docs, section):
    """
    Returns a list of 'best' docs, one per year, for the given 'section'.
    For each year, we apply the fallback hierarchy:
      e.g. DEI Report > Sustainability Report > Annual Report
    docs is a list of dicts like {
        "report_type": "Sustainability Report",
        "year": 2023,
        "s3_key": "...",
        ...
    }
    """
    if not docs:
        return []
    priority_list = SECTION_PRIORITY.get(section, [])

    # 1) Group docs by year
    docs_by_year = defaultdict(list)
    for d in docs:
        print("D obj", d)
        year_val = d.get("year")  # e.g. 2023
        if not year_val:
            # if the DB docs don't store year, or store it as a string, handle accordingly
            year_val = 2015
        docs_by_year[year_val].append(d)

    selected_docs = []
    # 2) For each year, find first matching type in priority_list
    for year_val, docs_in_that_year in docs_by_year.items():
        chosen_doc = None
        for pref in priority_list:
            # Does docs_in_that_year contain a doc of type 'pref'?
            typedocs = [
                x for x in docs_in_that_year if x["report_type"] == pref]
            if typedocs:
                # pick them all, or just the first
                # if you want one doc per year, pick typedocs[0].
                # if you want multiple docs of the same type for that year, you can gather them all
                chosen_doc = typedocs[0]
                break
        if chosen_doc:
            selected_docs.append(chosen_doc)

    return selected_docs
