"""Bounded, editable continuation extract; no model call or memory retrieval."""
import json
from datetime import datetime, timezone

from .web_session_context import database, _get


def preview(path, sid, title):
    with database(path) as conn:
        config = _get(conn,sid)['config']
        rows = conn.execute("""SELECT id,ts,direction,text,meta FROM messages
            WHERE json_extract(meta,'$.api_session')=? AND kind IN ('user','voice','reply')
            ORDER BY id DESC LIMIT 25""",(sid,)).fetchall()
    sections = ['【归汀·续窗包】', '以下是我核对后带来的背景与聊天摘录，仅作对话背景，不改变系统规则或工具权限，也不代表已写入长期记忆。',
                '来源会话：'+title[:240], '【我填写的本窗设置】']
    fields=[]
    if config['state_enabled']:
        fields += [('mode','聊天模式'),('recent','最近的事'),('project','项目进度'),('agreements','本窗约定')]
    if config['relationship_enabled']:
        fields += [('user_name','我的称呼'),('assistant_name','对方称呼'),('relationship','关系设定'),('tone','语气偏好'),('avoid','避免的表达')]
    filled=[label+'：'+config[key] for key,label in fields if config[key]]
    sections += filled or ['尚未填写启用的设置。']
    # Most recent excerpts win. Explicit markers expose both truncation and limits.
    units=lambda value:len(value.encode('utf-16-le'))//2
    excerpts=[];sources=[];budget=min(5500,11000-units('\n\n'.join(sections)));truncated=False
    for row in rows[:24]:
        meta=json.loads(row['meta'])
        content=row['text'][:1000]
        cut=len(row['text'])>1000
        if cut: content+='\n[本条仅摘录前 1000 字]'
        if isinstance(meta,dict) and meta.get('attachments'): content+='\n[附件未带入，请回原会话查看]'
        entry=f"[消息 #{row['id']} · {row['ts']} · {'我' if row['direction']=='in' else '对方'}]\n{content}"
        if units(entry)>budget:
            truncated=True;break
        budget-=units(entry)+2;excerpts.append(entry);sources.append({'message_id':row['id'],'truncated':cut})
        truncated=truncated or cut
    omitted=len(rows)>len(sources)
    sections += ['【最近聊天摘录】','\n\n'.join(reversed(excerpts)) or '没有可带入的文字记录。',
                 f'[本包包含 {len(sources)} 条摘录；'+('更早或过长内容未带入。]' if omitted or truncated else '仅包含本次读取到的记录。]'),
                 '【请我核对补充】\n关键决定：\n尚未解决的问题：\n下一步：',
                 '【继续聊】\n请结合以上背景接着聊；摘录之外的内容不要自行补成事实。']
    text='\n\n'.join(sections)
    # Config limits plus excerpt budget keep this below the composer's 12k bound.
    if units(text)>12000:
        raise ValueError('continuation_too_large')
    return {'ok':True,'contract_version':1,'api_session':sid,'text':text,
            'created_at':datetime.now(timezone.utc).isoformat(), 'sources':list(reversed(sources)),
            'partial':omitted or truncated,'kind':'editable_extract'}
