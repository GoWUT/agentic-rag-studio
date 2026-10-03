"""Quality checks have explicit fixture criteria; counts are not quality scores."""
import json
import re
from evaluation.multi_agent_efficiency.fixtures import GOAL


def quality(answer,evidence,citations,artifacts,grounding):
    text=answer.casefold()
    data=[e for e in evidence if e['source_type']=='analysis']
    document_names={e.get('document_name') for e in evidence if e['source_type']=='workspace'}
    ids={e['evidence_id'] for e in evidence}
    choices=[line for line in text.splitlines() if re.search(r'best|recommend|最佳|选择',line)]
    def names_a(line):
        plain=re.sub(r'[*`]', '',line)
        return re.search(r'\b(?:best|recommend\w*)\b[^.!?\n]{0,100}(?:\b(?:is|was)\b|:)\s*(?:(?:model|method|configuration)\s+)?a\b',plain) or re.search(r'\b(?:model|method|configuration)\s+a\b.{0,80}\b(?:is|was)\s+(?:the\s+)?best\b',plain)
    selected_a=bool(any(names_a(line) for line in choices) and '73.27' in text and '2.5' in text)
    excludes_b='3.2' in text and bool(re.search(r'exclud|over.budget|exceed|排除|超',text))
    descriptive=bool(re.search(r'not.{0,80}(?:causal|independent)|no causal|does not.{0,80}(?:caus|establish)|不能|不支持因果',text,re.S))
    coverage={'three_papers':all('paper_'+str(i)+'.pdf' in document_names for i in range(1,4)),
        'computed_dataset':bool(data),'chart_generated':any(a['mime_type']=='image/png' for a in artifacts)}
    valid=bool(citations) and all(c['evidence_id'] in ids for c in citations)
    return {'core_answer_correct':selected_a and excludes_b and descriptive,
        'criteria':{'select_A_2_5M_73_27':selected_a,'exclude_B_3_2M':excludes_b,'no_causal_claim':descriptive},
        'required_evidence_coverage':coverage,'citation_validity':valid,'grounded':grounding}
