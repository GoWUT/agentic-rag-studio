"""Small schema-checked pandas templates; ambiguous requests keep constrained generation."""
import re


def deterministic_code(objective, inspection):
    sheets = inspection.get('sheets', {})
    if len(sheets) != 1:
        return None
    table = next(iter(sheets.values()))
    columns = table['column_names']
    if re.search(r'correlation|regression|significan|anova|causal|相关|回归|显著', objective, re.I):
        return None
    numeric = [c for c in columns if re.search(r'int|float',table['dtypes'].get(c,''))]
    # Parameter units must be explicit in the schema: do not guess raw count vs millions.
    parameter = next((c for c in numeric if c.casefold() in {'params_m','parameters_m'}), None)
    metric = next((c for c in numeric if c.casefold() in objective.casefold() and c != parameter), None)
    remaining = [c for c in numeric if c != parameter]
    if metric is None and len(remaining)==1 and remaining[0].casefold() in {'miou','accuracy','f1','iou'}:
        metric = remaining[0]
    cap = re.search(r'(?:under|fewer than|below|less than|<|低于|小于)\s*(\d+(?:\.\d+)?)\s*[mM]', objective)
    if parameter and metric and cap and re.search(r'best|highest|max|最佳|最高', objective, re.I):
        chart = bool(re.search(r'chart|plot|图表|绘图', objective, re.I))
        code = f'''import json
import pandas as pd
frame = df.copy()
frame[{parameter!r}] = pd.to_numeric(frame[{parameter!r}], errors='coerce')
frame[{metric!r}] = pd.to_numeric(frame[{metric!r}], errors='coerce')
valid = frame.dropna(subset=[{parameter!r}, {metric!r}])
ranked = valid.sort_values({metric!r}, ascending=False)
eligible = ranked[ranked[{parameter!r}] < {float(cap[1])!r}]
summary = {{'best': json.loads(eligible.head(1).to_json(orient='records')), 'operation': 'rank_under_budget',
 'constraint': {{'operator': '<', 'threshold': {float(cap[1])!r}, 'unit': 'million parameters'}},
 'parameter_column': {parameter!r}, 'metric_column': {metric!r},
 'rows': len(frame), 'excluded_missing_rows': len(frame)-len(valid),
 'ranked': json.loads(ranked.head(20).to_json(orient='records')),
 'eligible': json.loads(eligible.head(20).to_json(orient='records')), 'eligible_count':len(eligible),
 'statistics': json.loads(valid.describe().to_json())}}
'''
        if chart:
            code += f'''import matplotlib.pyplot as plt
plt.figure(figsize=(6,4))
plt.scatter(valid[{parameter!r}], valid[{metric!r}])
plt.axvline({float(cap[1])!r}, linestyle='--')
plt.xlabel({parameter!r})
plt.ylabel({metric!r})
plt.tight_layout()
plt.savefig('comparison.png')
plt.close()
summary['chart'] = 'comparison.png'
'''
        return code + "write_artifact('summary.json', json.dumps(summary, indent=2))\nprint(json.dumps(summary))"
    # These templates require one named numeric column and a single operation.
    matches = [c for c in numeric if re.search(r'(?<!\w)'+re.escape(c)+r'(?!\w)', objective, re.I)]
    if len(matches)==1 and not re.search(r'and|then|chart|plot|filter|compare|并且|然后', objective, re.I):
        operation = next((op for op,pattern in [('mean',r'average|mean|平均'),('min',r'minimum|\bmin\b|最小'),
            ('max',r'maximum|\bmax\b|最大')] if re.search(pattern,objective,re.I)), None)
        if operation:
            return f"import json\nprint(json.dumps({{'column':{matches[0]!r},'operation':{operation!r},'value':df[{matches[0]!r}].{operation}()}}))"
    if re.fullmatch(r'(?:how many rows\??|row count|count rows|多少行[？?]?)', objective.strip(), re.I):
        return "import json\nprint(json.dumps({'rows':len(df)}))"
    if len(matches)==1 and re.fullmatch(r'(?:sort|排序)\s+'+re.escape(matches[0])+r'\s*(?:ascending|descending|升序|降序)?',objective.strip(),re.I):
        descending = bool(re.search('descending|降序',objective,re.I))
        return f"print(df.sort_values({matches[0]!r},ascending={not descending!r}).to_json(orient='records'))"
    return None
