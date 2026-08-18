import React, { useState, useRef, useEffect } from 'react';
import './App.css';

function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [threadId, setThreadId] = useState('user_web_' + Date.now());
  const messagesEndRef = useRef(null);

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async () => {
    if (!input.trim() || isLoading) return;

    const userMsg = { role: 'user', content: input };
    setMessages(prev => [...prev, userMsg]);
    setInput('');
    setIsLoading(true);

    // 添加占位 AI 消息
    const aiMsgIndex = messages.length + 1;
    setMessages(prev => [...prev, { role: 'assistant', content: '' }]);

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: input,
          thread_id: threadId,
        }),
      });

      if (!response.ok || !response.body) {
        throw new Error(`请求失败 (${response.status})`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let fullContent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));
              if (data.type === 'text') {
                fullContent += data.content;
                // 更新最后一条消息
                setMessages(prev => {
                  const newMsgs = [...prev];
                  if (newMsgs[newMsgs.length - 1].role === 'assistant') {
                    newMsgs[newMsgs.length - 1].content = fullContent;
                  }
                  return newMsgs;
                });
              } else if (data.type === 'tool') {
                // 显示工具状态提示
                const toolStatus = data.status === 'calling' ? '🔧 调用工具...' : '✅ 工具完成';
                setMessages(prev => {
                  const newMsgs = [...prev];
                  if (newMsgs[newMsgs.length - 1].role === 'assistant') {
                    newMsgs[newMsgs.length - 1].content = fullContent + '\n' + toolStatus;
                  }
                  return newMsgs;
                });
              } else if (data.type === 'error') {
                alert('错误: ' + data.content);
              }
            } catch (e) {
              console.warn('解析 SSE 数据失败:', e);
            }
          }
        }
      }
    } catch (error) {
      console.error('请求失败:', error);
      setMessages(prev => {
        const newMsgs = [...prev];
        if (newMsgs[newMsgs.length - 1].role === 'assistant') {
          newMsgs[newMsgs.length - 1].content = '❌ 网络错误，请重试';
        }
        return newMsgs;
      });
    }

    setIsLoading(false);
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
          <div ref={messagesEndRef} />
        </div>
        <div className="input-area">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && sendMessage()}
            placeholder="输入消息..."
            disabled={isLoading}
          />
          <button onClick={sendMessage} disabled={isLoading}>
            {isLoading ? '发送中...' : '发送'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default App;
