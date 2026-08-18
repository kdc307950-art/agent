#工具函数
import ast
import operator
from langchain_core.tools import tool


@tool
def get_weather(location: str) -> str:
    """获取指定城市的天气（模拟）"""
    return f"{location}的天气是晴天，气温25°C"


@tool
def calculate(expression: str) -> str:
    """
    安全计算数学表达式（仅支持 + - * / 和数字）
    例如: '2 + 2' 或 '10 * 5'
    """
    # 只允许安全的字符：数字、运算符、括号、空格、小数点
    allowed_chars = set("0123456789+-*/() .")
    if not all(c in allowed_chars for c in expression):
        return "错误：表达式包含非法字符"

    try:
        # 使用 ast.literal_eval 配合运算节点解析（安全可靠）
        # 构建安全的解析树
        tree = ast.parse(expression, mode='eval')
        # 只允许特定的节点类型
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Expression, ast.BinOp, ast.Add,
                                     ast.Sub, ast.Mult, ast.Div, ast.Constant,
                                     ast.Load, ast.UnaryOp, ast.USub, ast.UAdd)):
                return "错误：不支持的运算符或语法"

        # 编译并执行（在受限命名空间中）
        code = compile(tree, '<string>', 'eval')
        result = eval(code, {"__builtins__": {}}, {})
        return f"计算结果: {result}"
    except SyntaxError:
        return "错误：表达式语法不正确"
    except ZeroDivisionError:
        return "错误：除数不能为零"
    except Exception as e:
        return f"计算错误: {str(e)}"


tools = [get_weather, calculate]