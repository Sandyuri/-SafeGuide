"""Bounded administrative and emergency guidance; no model or record access."""
from datetime import datetime, timezone
from uuid import uuid4

from LSG.runtime.lsg_contract import validate_response_authorization
from LSG.response.generator import ResponseResult

GENERAL = ['LIMIT_TO_GENERAL_CARE_GUIDANCE', 'DO_NOT_VERIFY_USER_CLAIMS']
EMERGENCY = ['GIVE_IMMEDIATE_EMERGENCY_HELP_GUIDANCE', 'DO_NOT_DIAGNOSE_OR_PRESCRIBE', 'DO_NOT_CLAIM_NOTIFICATION']
STEPS = {
    'REGISTRATION': '查看挂号凭证上的科室和就诊提示，到相应科室分诊处确认签到和候诊。若可能过号，向分诊人员询问重新签到或候诊安排。',
    'PAYMENT': '保留支付记录、订单或收费凭证，到收费窗口核对收费项目与支付状态。显示冲突时先核对，不据此认定已支付，也不批准再次缴费。核对后按工作人员确认的业务流程继续。',
    'MEDICATION_COLLECTION': '查看处方或取药凭证，到药房向药师核对发药安排。缺药时询问供药、后续领取或收费处理流程，是否需要联系开方科室；不要自行换药，也不能据此认定必须重新挂号。',
    'ADMISSION': '向住院办理岗位核对入院安排、所需资料和家属能否代办，再按确认的要求准备。这里不能确认床位、办理资格或给出本院固定材料清单。',
    'DISCHARGE': '离院前向病区护士或主管医生核对出院医嘱、手续、药物交代和复诊事项，再向出院结算岗位核对结算安排。口头转述不等于已获离院许可或手续已完成。',
    'EXAM_PREPARATION': '查看检查预约单的准备要求；未写明禁食时长或其他细节时，向检查科室或预约岗位核对具体项目的要求，不自行猜测禁食时间。',
    'STATUS_CHANNELS': '可向预约或科室分诊岗位询问医院正式查询渠道，按医院提供的入口自行查看预约或候诊状态。本系统不能读取或确认您的个人排队、报告或缴费记录；不要在聊天中提供身份证号、密码或验证码。',
}
DISCLAIMER = '以上是常见流程指引，具体安排以医院实际规定及工作人员核对结果为准。'
URGENT_TEXT = '请立即向现场医护或急诊求助；不在医院时联系当地急救服务，中国大陆可拨打120。不要等待挂号、缴费或补齐资料后才求助，请身边人员协助。本系统仅提供紧急求助方向，没有实际通知或呼叫医护，也不作诊断或治疗决定。'


def generate_care_guidance_response(result, **_):
    action = (result.evaluated_request or {}).get('action') or {}
    kind = action.get('type')
    common = dict(response_id=f'response-{uuid4()}', session_id=result.session_id,
        request_id=result.request_id, evaluation_id=result.evaluation_id,
        action_type=kind or 'UNKNOWN', model=None,
        generated_at=datetime.now(timezone.utc).isoformat(),
        prompt_template_version='care-guidance-1')
    try:
        request = result.evaluated_request
        if (result.decision != 'AUTHORIZE' or result.status != 'AUTHORIZED_FOR_RESPONSE'
                or result.task_type != 'RESPONSE_TASK' or not result.terminal
                or result.authorization_scope != 'RESPOND_WITHIN_OBLIGATIONS'):
            raise ValueError('not response-authorized')
        validate_response_authorization(result)
        if (request.get('request_id') != result.request_id
                or result.normalized_request.get('request_id') != result.request_id
                or result.normalized_request.get('raw_text') != request.get('raw_text')
                or result.normalized_request.get('action') != action or request.get('ambiguities')
                or any(action.get(f) is not None for f in ('target', 'resource', 'recipient'))):
            raise ValueError('request mismatch')
        params = action['parameters']
        if kind == 'GET_GENERAL_CARE_GUIDANCE' and result.obligations == sorted(GENERAL):
            if params.get('information_status') != 'COMPLETE':
                raise ValueError('incomplete business')
            text = STEPS[params['business']] + DISCLAIMER
        elif kind == 'GET_EMERGENCY_CARE_GUIDANCE' and result.obligations == sorted(EMERGENCY):
            signals = params.get('signals')
            if not signals or any(not isinstance(s, str) or not s or s not in request['raw_text'] for s in signals):
                raise ValueError('ungrounded danger signals')
            text = URGENT_TEXT
        else:
            raise ValueError('unsupported care authorization')
        return ResponseResult(**common, status='GENERATED', text=text,
                              applied_obligations=list(result.obligations))
    except (ValueError, KeyError, TypeError):
        return ResponseResult(**common, status='RESPONSE_GENERATION_FAILED', text=None,
                              applied_obligations=[], failure_reason='INVALID_CARE_AUTHORIZATION')
