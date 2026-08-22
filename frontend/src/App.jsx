import React, { useState, useRef, useEffect } from 'react';
import './App.css';

function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [threadId, setThreadId] = useState('user_web_' + Date.now());
  // 图停在 human_approval 节点时的待审批信息；非空时输入框锁定，只能批准/拒绝
  const [pendingApproval, setPendingApproval] = useState(null);
  const messagesEndRef = useRef(null);

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, pendingApproval]);

  // 读一条 SSE 流。/chat/stream 与 /chat/resume 的事件协议完全一致，所以共用这段。
  const runStream = async (url, body) => {
    setIsLoading(true);
    setMessages(prev => [...prev, { role: 'assistant', content: '' }]);
    let fullContent = '';

    const updateLast = (text) => {
      setMessages(prev => {
        const newMsgs = [...prev];
        if (newMsgs[newMsgs.length - 1].role === 'assistant') {
          newMsgs[newMsgs.length - 1].content = text;
        }
        return newMsgs;
      });
    };

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      if (!response.ok || !response.body) {
        throw new Error(`请求失败 (${response.status})`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));
            if (data.type === 'text') {
              fullContent += data.content;
              updateLast(fullContent);
            } else if (data.type === 'tool') {
              // 显示工具状态提示
              const toolStatus = data.status === 'calling' ? '🔧 调用工具...' : '✅ 工具完成';
              updateLast(fullContent + '\n' + toolStatus);
            } else if (data.type === 'interrupt') {
              // 后端到此为止且不会再发 end，等用户决定后调 /chat/resume 继续同一个线程
              setPendingApproval({
                question: data.question,
                interruptId: data.interrupt_id,
                runId: data.run_id,
              });
              updateLast(fullContent || '⏸ 等待人工审批');
            } else if (data.type === 'error') {
              alert('错误: ' + data.content);
            }
          } catch (e) {
            console.warn('解析 SSE 数据失败:', e);
          }
        }
      }
    } catch (error) {
      console.error('请求失败:', error);
      updateLast('❌ 网络错误，请重试');
    }

    setIsLoading(false);
  };

  const sendMessage = async () => {
    if (!input.trim() || isLoading || pendingApproval) return;
    const text = input;
    setMessages(prev => [...prev, { role: 'user', content: text }]);
    setInput('');
    await runStream('/api/chat/stream', { message: text, thread_id: threadId });
  };

  const submitApproval = async (approved) => {
    if (!pendingApproval || isLoading) return;
    const approval = pendingApproval;
    setPendingApproval(null);
    setMessages(prev => [...prev, { role: 'user', content: approved ? '✅ 批准' : '🚫 拒绝' }]);
    await runStream('/api/chat/resume', {
      thread_id: threadId,
      approved,
      interrupt_id: approval.interruptId,
      resumed_from: approval.runId,
    });
  };

  return (
    <div className="App">
      <div className="chat-container">
        <header className="chat-header">
          <h1>🤖 LangGraph Agent</h1>
          <span className="thread-id">会话: {threadId.slice(-8)}</span>
        </header>
        <div className="messages">
          {messages.map((msg, idx) => (
            <div key={idx} className={`message ${msg.role}`}>
              <div className="message-content">{msg.content || '...'}</div>
            </div>
          ))}
          {isLoading && <div className="typing-indicator">正在思考...</div>}
          {pendingApproval && (
            <div className="approval-card">
              <div className="approval-question">⏸ {pendingApproval.question}</div>
              <div className="approval-actions">
                <button onClick={() => submitApproval(true)} disabled={isLoading}>
                  批准
                </button>
                <button className="reject" onClick={() => submitApproval(false)} disabled={isLoading}>
                  拒绝
                </button>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>
        <div className="input-area">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && sendMessage()}
            placeholder={pendingApproval ? '请先处理待审批的操作…' : '输入消息...'}
            disabled={isLoading || !!pendingApproval}
          />
          <button onClick={sendMessage} disabled={isLoading || !!pendingApproval}>
            {isLoading ? '发送中...' : '发送'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default App;
