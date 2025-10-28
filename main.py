import re
import sys
import os
import time
import gc
import traceback
from typing import List, Dict, Any, Tuple, Set, Optional, Callable, Union, Pattern, Match, Generator
from dataclasses import dataclass
from contextlib import contextmanager

@dataclass
class Scope:
    locals: Set[str]
    globals: Set[str]
    functions: Dict[str, int]
    line_start: int

class LuaEndToPy:
    def __init__(self):
        self.lines: List[str] = []
        self.py_lines: List[str] = []
        self.imports: Set[str] = set()
        self.warnings: List[str] = []
        self.errors: List[str] = []
        self.used_modules: Set[str] = set()
        self.stack: List[Dict[str, Any]] = []
        self.scopes: List[Scope] = [Scope(set(), set(), {}, 0)]
        self.current_line = 0
        self.max_lines = 3000000
        self.gg_functions: Set[str] = set()
        self.roblox_functions: Set[str] = set()
        self.metatables: Dict[str, str] = {}
        self.string_cache: Dict[str, str] = {}
        self.regex_cache: Dict[str, Pattern] = {}
        self.label_map: Dict[str, int] = {}
        self.goto_targets: List[Tuple[int, str]] = []
        self.function_depth = 0
        self.indent_cache: Dict[int, str] = {}
        self.var_counter = 0

    def load_file(self, path: str) -> None:
        with open(path, 'rb') as f:
            raw = f.read()
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]
        encodings = ['utf-8', 'cp1251', 'cp1252', 'latin1', 'ascii']
        content = None
        for enc in encodings:
            try:
                content = raw.decode(enc)
                break
            except Exception:
                continue
        if content is None:
            content = raw.decode('utf-8', errors='replace')
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        self.lines = [line.rstrip('\n') for line in content.splitlines()]
        if len(self.lines) > self.max_lines:
            self.warnings.append(f"Файл очень большой ({len(self.lines)} строк)")

    def add_import(self, module: str) -> None:
        self.imports.add(module)

    def get_indent(self, level: int) -> str:
        if level not in self.indent_cache:
            self.indent_cache[level] = ' ' * level
        return self.indent_cache[level]

    def compile_regex(self, pattern: str) -> Pattern:
        if pattern not in self.regex_cache:
            self.regex_cache[pattern] = re.compile(pattern, re.DOTALL)
        return self.regex_cache[pattern]

    @contextmanager
    def scope_context(self):
        self.scopes.append(Scope(set(), set(), {}, self.current_line))
        try:
            yield
        finally:
            self.scopes.pop()

    def extract_multiline(self, start: int, is_comment: bool) -> Tuple[str, int]:
        opener = '--[[' if is_comment else '[['
        closer = ']]'
        content_lines = []
        i = start
        current = self.lines[i]
        pos = len(opener)
        if len(current) > pos:
            remainder = current[pos:].rstrip()
            if closer in remainder:
                parts = remainder.split(closer, 1)
                return parts[0], i + 1
            content_lines.append(remainder)
        else:
            content_lines.append('')
        i += 1
        nesting = 1
        while i < len(self.lines) and nesting > 0:
            current = self.lines[i]
            if current.strip().startswith(opener):
                nesting += 1
            if closer in current:
                nesting -= 1
                if nesting == 0:
                    parts = current.split(closer, 1)
                    content_lines.append(parts[0])
                    return '\n'.join(content_lines), i + 1
            content_lines.append(current)
            i += 1
        self.warnings.append(f"Многострочный {'комментарий' if is_comment else 'строка'} не закрыт (строка {start + 1})")
        return '\n'.join(content_lines), i

    def new_temp_var(self) -> str:
        name = f"_tmp_{self.var_counter}"
        self.var_counter += 1
        return name

    def apply_replacements(self, code: str) -> str:
        patterns = [
            (r'\bnil\b', 'None'),
            (r'\btrue\b', 'True'),
            (r'\bfalse\b', 'False'),
            (r'Color3\.fromRGB\s*\(', 'Color3.from_rgb('),
            (r'UDim2\.new\s*\(', 'UDim2.new('),
            (r'UDim\.new\s*\(', 'UDim.new('),
            (r'Instance\.new\s*\(\s*"([^"]+)"\s*(?:,\s*(.*?))?\s*\)', r'Instance.new("\1", \2)'),
            (r':GetService\s*\(\s*"([^"]+)"\s*\)', r'.get_service("\1")'),
            (r'game\s*:\s*', 'game.'),
            (r':(\w+)\s*\(', r'.\1('),
            (r'Players\.LocalPlayer\b', 'players.local_player'),
            (r':Connect\s*\(\s*function\s*\(\s*\)\s*(.*?)\s*end\s*\)', r'.connect(lambda: \1)'),
            (r':Fire\s*\(\s*(.*?)\s*\)', r'.fire(\1)'),
            (r'math\.random\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)', r'random.randint(\1, \2)'),
            (r'\.\.\s*', ' + '),
            (r'pcall\s*\(\s*function\s*\(\s*\)\s*(.*?)\s*end\s*\)', r'__pcall_wrapper(lambda: \1)'),
            (r'pcall\s*\(\s*(.+?)\s*\)', r'__pcall_wrapper(\1)'),
            (r'xpcall\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', r'__xpcall_wrapper(\1, \2)'),
            (r'error\s*\(\s*(.*?)\s*,\s*(\d+)\s*\)', r'raise RuntimeError(\1)'),
            (r'error\s*\(\s*(.*?)\s*\)', r'raise RuntimeError(\1)'),
            (r'type\s*\(\s*(.*?)\s*\)', r'type(\1)'),
            (r'assert\s*\(\s*(.+?)\s*\)', r'__assert_wrapper(\1)'),
            (r'(\w+)\s+or\s+(.+?)(?=\s*[),;}|]|\s|$)', r'\1 if \1 is not None else \2'),
            (r'#(\w+)', r'len(\1)'),
            (r'~=', '!='),
            (r'table\.insert\s*\(\s*(\w+)\s*,\s*(\d+)\s*,\s*(.+?)\s*\)', r'\1.insert(int(\2)-1, \3)'),
            (r'table\.insert\s*\(\s*(\w+)\s*,\s*(.+?)\s*\)', r'\1.append(\2)'),
            (r'table\.remove\s*\(\s*(\w+)\s*,\s*(\d+)\s*\)', r'\1.pop(int(\2)-1)'),
            (r'table\.remove\s*\(\s*(\w+)\s*\)', r'\1.pop()'),
            (r'table\.concat\s*\(\s*(\w+)\s*,\s*"([^"]*)"\s*(?:,\s*(\d+)\s*,\s*(\d+)\s*)?\)', r'"\2".join(str(\1[i]) for i in range(int(\3 or 1)-1, min(int(\4 or len(\1))+1, len(\1))))'),
            (r'table\.concat\s*\(\s*(\w+)\s*,\s*"([^"]*)"\s*\)', r'"\2".join(map(str, \1))'),
            (r'table\.sort\s*\(\s*(\w+)\s*(?:,\s*(function\s*\(.*?\)\s*.*?end|[\w\.]+))?\s*\)', lambda m: self._table_sort(m.group(1), m.group(2))),
            (r'setmetatable\s*\(\s*(\w+)\s*,\s*(\{[^}]*\})\s*\)', lambda m: self._handle_setmetatable(m.group(1), m.group(2))),
            (r'getmetatable\s*\(\s*(\w+)\s*\)', r'__getmetatable(\1)'),
            (r'coroutine\.create\s*\(\s*(.+?)\s*\)', r'threading.Thread(target=\1, daemon=True)'),
            (r'coroutine\.resume\s*\(\s*(\w+)\s*\)', r'\1.start()'),
            (r'coroutine\.yield\s*\(\s*(.*?)\s*\)', r'__yield(\1)'),
            (r'coroutine\.wrap\s*\(\s*(.+?)\s*\)', r'lambda *a, **k: __coroutine_wrap(\1)(*a, **k)'),
            (r'string\.format\s*\(\s*([^,]+)\s*,(.*)\)', lambda m: self._format_string(m.group(1), m.group(2))),
            (r'string\.byte\s*\(\s*(\w+)\s*,\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)', lambda m: self._string_byte(m.group(1), m.group(2), m.group(3))),
            (r'string\.char\s*\(\s*(.+?)\s*\)', lambda m: self._string_char(m.group(1))),
            (r'string\.gsub\s*\(\s*(\w+)\s*,\s*"(.*?)"\s*,\s*"(.*?)"\s*(?:,\s*(\d+)\s*)?\)', r'\1.replace("\2", "\3", \4 or -1)'),
            (r'string\.find\s*\(\s*(\w+)\s*,\s*"(.*?)"\s*(?:,\s*(\d+)\s*)?\)', r'\1.find("\2", \3 or 0) + 1'),
            (r'string\.match\s*\(\s*(\w+)\s*,\s*"([^"]*)"\s*\)', r're.search(r"\2", \1).group() if re.search(r"\2", \1) else None'),
            (r'string\.upper\s*\(\s*(\w+)\s*\)', r'\1.upper()'),
            (r'string\.lower\s*\(\s*(\w+)\s*\)', r'\1.lower()'),
            (r'math\.floor\s*\(\s*(.+?)\s*\)', r'math.floor(\1)'),
            (r'math\.ceil\s*\(\s*(.+?)\s*\)', r'math.ceil(\1)'),
            (r'math\.pi\b', 'math.pi'),
            (r'math\.sin\s*\(\s*(.+?)\s*\)', r'math.sin(\1)'),
            (r'math\.cos\s*\(\s*(.+?)\s*\)', r'math.cos(\1)'),
            (r'math\.tan\s*\(\s*(.+?)\s*\)', r'math.tan(\1)'),
            (r'math\.log\s*\(\s*(.+?)\s*(?:,\s*(.+?))?\s*\)', r'math.log(\1, \2 or math.e)'),
            (r'math\.sqrt\s*\(\s*(.+?)\s*\)', r'math.sqrt(\1)'),
            (r'math\.abs\s*\(\s*(.+?)\s*\)', r'abs(\1)'),
            (r'math\.rad\s*\(\s*(.+?)\s*\)', r'math.radians(\1)'),
            (r'math\.deg\s*\(\s*(.+?)\s*\)', r'math.degrees(\1)'),
            (r'math\.huge\b', 'float("inf")'),
            (r'os\.time\s*\(\s*(\{.*\})\s*\)', r'time.mktime(time.struct_time([int(\1.get(k, 0)) for k in ["tm_year","tm_mon","tm_mday","tm_hour","tm_min","tm_sec","tm_wday","tm_yday","tm_isdst"]]]))'),
            (r'os\.time\s*\(\s*\)', 'time.time()'),
            (r'os\.date\s*\(\s*"(.*?)"\s*(?:,\s*(.+?))?\s*\)', r'time.strftime("\1", time.localtime(\2 or time.time()))'),
            (r'os\.clock\s*\(\s*\)', 'time.perf_counter()'),
            (r'io\.open\s*\(\s*"(.*?)"\s*,\s*"(.*?)"\s*\)', r'open("\1", "\2")'),
            (r'require\s*\(\s*[\'"](\w+)[\'"]\s*\)', lambda m: self._handle_require(m.group(1))),
            (r'unpack\s*\(\s*(\w+)\s*\)', r'*(\1 if hasattr(\1, "__iter__") and not isinstance(\1, str) else list(\1))'),
            (r'_ENV\b', 'globals()'),
            (r'collectgarbage\s*\(\s*"collect"\s*\)', 'gc.collect()'),
            (r'bit32\.band\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', r'\1 & \2'),
            (r'bit32\.bor\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', r'\1 | \2'),
            (r'bit32\.bxor\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', r'\1 ^ \2'),
            (r'bit32\.lshift\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', r'\1 << \2'),
            (r'bit32\.rshift\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', r'\1 >> \2'),
            (r'goto\s+(\w+)', lambda m: self._handle_goto(m.group(1))),
            (r'::(\w+)::', lambda m: self._handle_label(m.group(1))),
            (r'loadstring\s*\(\s*(.+?)\s*\)', r'compile(\1, "<lua>", "exec")'),
            (r'dofile\s*\(\s*"(.*?)"\s*\)', r'exec(open("\1", encoding="utf-8").read())'),
            (r'wait\s*\(\s*([\d.]+)\s*\)', r'time.sleep(\1)'),
            (r'print\s*\(\s*(.*?)\s*\)', r'print(\1)'),
            (r'pairs\s*\(\s*(\w+)\s*\)', r'\1.items()'),
            (r'ipairs\s*\(\s*(\w+)\s*\)', r'enumerate(\1)'),
            (r'rawset\s*\(\s*(\w+)\s*,\s*(.+?)\s*,\s*(.+?)\s*\)', r'\1[\2] = \3'),
            (r'rawget\s*\(\s*(\w+)\s*,\s*(.+?)\s*\)', r'\1.get(\2)'),
            (r'rawlen\s*\(\s*(\w+)\s*\)', r'len(\1)'),
            (r'next\s*\(\s*(\w+)\s*(?:,\s*(.+?))?\s*\)', r'next(iter(\1), \2)'),
            (r'tostring\s*\(\s*(.+?)\s*\)', r'str(\1)'),
            (r'tonumber\s*\(\s*(.+?)\s*\)', r'float(\1) if \1 else 0'),
            (r'getfenv\s*\(\s*(\d+)\s*\)', r'__getfenv(\1)'),
            (r'setfenv\s*\(\s*(\d+)\s*,\s*(\w+)\s*\)', r'__setfenv(\1, \2)'),
            (r'debug\.getinfo\s*\(\s*(\d+)\s*\)', r'__debug_getinfo(\1)'),
            (r'debug\.traceback\s*\(\s*\)', r'traceback.format_exc()'),
            (r'debug\.getupvalue\s*\(\s*(.+?)\s*,\s*(\d+)\s*\)', r'__getupvalue(\1, \2)'),
            (r'debug\.setupvalue\s*\(\s*(.+?)\s*,\s*(\d+)\s*,\s*(.+?)\s*\)', r'__setupvalue(\1, \2, \3)'),
            (r'load\s*\(\s*function\s*\(\s*\)\s*(.*?)\s*end\s*\)', r'compile(\1, "<lua>", "exec")'),
        ]
        for pattern, repl in patterns:
            regex = self.compile_regex(pattern)
            if callable(repl):
                code = regex.sub(repl, code)
            else:
                code = regex.sub(repl, code)

        roblox_props = {
            r'\.Parent\s*=': '.parent =',
            r'\.Name\s*=': '.name =',
            r'\.Size\s*=': '.size =',
            r'\.Position\s*=': '.position =',
            r'\.BackgroundColor3\s*=': '.background_color3 =',
            r'\.Text\s*=': '.text =',
            r'\.TextColor3\s*=': '.text_color3 =',
            r'\.Visible\s*=': '.visible =',
            r'\.Transparency\s*=': '.transparency =',
            r':Wait\s*\(\s*\)': '.wait()',
            r':Destroy\s*\(\s*\)': '.destroy()',
            r':Clone\s*\(\s*\)': '.clone()',
            r':FindFirstChild\s*\(\s*"([^"]+)"\s*(?:,\s*(true|false)\s*)?\)': r'.find_first_child("\1", \2 == "true" if \2 else False)',
            r':WaitForChild\s*\(\s*"([^"]+)"\s*(?:,\s*([\d.]+)\s*)?\)': r'.wait_for_child("\1", float(\2) if \2 else None)',
            r':GetChildren\s*\(\s*\)': '.get_children()',
            r':GetDescendants\s*\(\s*\)': '.get_descendants()',
            r':IsA\s*\(\s*"([^"]+)"\s*\)': r'.is_a("\1")',
            r':TweenSize\s*\(\s*(.+?)\s*\)': r'.tween_size(\1)',
            r':TweenPosition\s*\(\s*(.+?)\s*\)': r'.tween_position(\1)',
            r':GetPropertyChangedSignal\s*\(\s*"([^"]+)"\s*\)': r'.get_property_changed_signal("\1")',
            r':BindToRenderStep\s*\(\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(.+?)\s*\)': r'.bind_to_render_step("\1", int(\2), \3)',
            r':UnbindFromRenderStep\s*\(\s*"([^"]+)"\s*\)': r'.unbind_from_render_step("\1")',
            r'workspace\b': 'workspace',
            r'game\.GetService\s*\(\s*"([^"]+)"\s*\)': r'game.get_service("\1")',
            r'Enum\.([A-Za-z]+)\.([A-Za-z]+)\b': r'Enum.\1.\2',
            r'Vector3\.new\s*\(\s*([\d\.\-\+e]+)\s*,\s*([\d\.\-\+e]+)\s*,\s*([\d\.\-\+e]+)\s*\)': r'Vector3(float(\1), float(\2), float(\3))',
            r'CFrame\.new\s*\(\s*([\d\.\-\+e]+)\s*,\s*([\d\.\-\+e]+)\s*,\s*([\d\.\-\+e]+)\s*\)': r'CFrame(float(\1), float(\2), float(\3))',
            r'BrickColor\.new\s*\(\s*"([^"]+)"\s*\)': r'BrickColor("\1")',
            r'tick\s*\(\s*\)': 'time.time()',
            r'spawn\s*\(\s*(.+?)\s*\)': r'threading.Thread(target=lambda: (\1), daemon=True).start()',
            r'delay\s*\(\s*([\d.]+)\s*,\s*(.+?)\s*\)': r'threading.Timer(float(\1), lambda: (\2)).start()',
            r'game:Clone\s*\(\s*\)': 'game.clone()',
            r'game:ClearAllChildren\s*\(\s*\)': 'game.clear_all_children()',
            r'game:IsLoaded\s*\(\s*\)': 'game.is_loaded()',
        }
        for old, new in roblox_props.items():
            code = re.sub(old, new, code, flags=re.IGNORECASE)
            if re.search(old, code, re.IGNORECASE):
                self.roblox_functions.add(old.split('\\')[0] if '\\' in old else old)

        gg_patterns = [
            (r'gg\.getRanges\s*\(\s*\)', 'gg.get_ranges()'),
            (r'gg\.setRanges\s*\(\s*(.+?)\s*\)', 'gg.set_ranges(\1)'),
            (r'gg\.getRangesList\s*\(\s*\)', 'gg.get_ranges_list()'),
            (r'gg\.getRangesList\s*\(\s*"([^"]+)"\s*\)', 'gg.get_ranges_list("\1")'),
            (r'gg\.searchNumber\s*\(\s*"([^"]+)"\s*,\s*gg\.TYPE_(\w+)\s*(?:,\s*(.+?))?\s*\)', lambda m: self._gg_search_number(m.group(1), m.group(2), m.group(3) or '')),
            (r'gg\.searchFuzzy\s*\(\s*"([^"]+)"\s*,\s*gg\.TYPE_(\w+)\s*(?:,\s*(.+?))?\s*\)', lambda m: self._gg_search_fuzzy(m.group(1), m.group(2), m.group(3) or '')),
            (r'gg\.getResults\s*\(\s*(\d*)\s*(?:,\s*(\d+)\s*)?\)', r'gg.get_results(int(\1) if \1 else 1000, int(\2) if \2 else None)'),
            (r'gg\.editAll\s*\(\s*"([^"]+)"\s*,\s*gg\.TYPE_(\w+)\s*\)', r'gg.edit_all("\1", gg.TYPE_\2)'),
            (r'gg\.addListItems\s*\(\s*(\w+)\s*\)', r'gg.add_list_items(\1)'),
            (r'gg\.removeListItems\s*\(\s*(\w+)\s*\)', r'gg.remove_list_items(\1)'),
            (r'gg\.clearResults\s*\(\s*\)', 'gg.clear_results()'),
            (r'gg\.setVisible\s*\(\s*(true|false)\s*\)', r'gg.set_visible(\1.lower() == "true")'),
            (r'gg\.isVisible\s*\(\s*\)', 'gg.is_visible()'),
            (r'gg\.toast\s*\(\s*"([^"]+)"\s*\)', r'gg.toast("\1")'),
            (r'gg\.alert\s*\(\s*"([^"]+)"\s*(?:,\s*"([^"]*)")?\s*\)', r'gg.alert("\1", "\2")'),
            (r'gg\.prompt\s*\(\s*(\[.*?\])\s*,\s*(\[.*?\])\s*(?:,\s*(\[.*?\]))?\s*\)', r'gg.prompt(\1, \2, \3 or None)'),
            (r'gg\.choice\s*\(\s*(\[.*?\])\s*(?:,\s*(\d+))?\s*(?:,\s*"([^"]*)")?\s*\)', r'gg.choice(\1, int(\2) if \2 else None, "\3")'),
            (r'gg\.multiChoice\s*\(\s*(\[.*?\])\s*(?:,\s*(\[.*?\]))?\s*\)', r'gg.multi_choice(\1, \2 or None)'),
            (r'gg\.sleep\s*\(\s*(\d+)\s*\)', r'time.sleep(int(\1) / 1000)'),
            (r'gg\.saveVariable\s*\(\s*(\w+)\s*,\s*"([^"]+)"\s*\)', r'gg.save_variable(\1, "\2")'),
            (r'gg\.loadVariable\s*\(\s*"([^"]+)"\s*\)', r'gg.load_variable("\1")'),
            (r'gg\.processOpen\s*\(\s*\)', 'gg.process_open()'),
            (r'gg\.processClose\s*\(\s*\)', 'gg.process_close()'),
            (r'gg\.processKill\s*\(\s*\)', 'gg.process_kill()'),
            (r'gg\.processPause\s*\(\s*\)', 'gg.process_pause()'),
            (r'gg\.processResume\s*\(\s*\)', 'gg.process_resume()'),
            (r'gg\.getTargetInfo\s*\(\s*\)', 'gg.get_target_info()'),
            (r'gg\.getTargetPackage\s*\(\s*\)', 'gg.get_target_package()'),
            (r'gg\.setSpeed\s*\(\s*([\d.]+)\s*\)', r'gg.set_speed(float(\1))'),
            (r'gg\.isPackageInstalled\s*\(\s*"([^"]+)"\s*\)', r'gg.is_package_installed("\1")'),
            (r'gg\.getFile\s*\(\s*\)', 'gg.get_file()'),
            (r'gg\.copyText\s*\(\s*"([^"]+)"\s*\)', r'gg.copy_text("\1")'),
            (r'gg\.makeRequest\s*\(\s*"([^"]+)"\s*\)', r'gg.make_request("\1")'),
            (r'gg\.setValues\s*\(\s*(\w+)\s*\)', r'gg.set_values(\1)'),
            (r'gg\.loadList\s*\(\s*"([^"]+)"\s*\)', r'gg.load_list("\1")'),
            (r'gg\.saveList\s*\(\s*"([^"]+)"\s*,\s*(\w+)\s*\)', r'gg.save_list("\1", \2)'),
            (r'gg\.getLine\s*\(\s*\)', 'gg.get_line()'),
            (r'gg\.getLocale\s*\(\s*\)', 'gg.get_locale()'),
            (r'gg\.setLocale\s*\(\s*"([^"]+)"\s*\)', r'gg.set_locale("\1")'),
            (r'gg\.getVersion\s*\(\s*\)', 'gg.get_version()'),
            (r'gg\.getVersionCode\s*\(\s*\)', 'gg.get_version_code()'),
            (r'gg\.require\s*\(\s*([\d.]+)\s*\)', r'gg.require(\1)'),
            (r'gg\.TYPE_AUTO\b', 'gg.TYPE_AUTO'),
            (r'gg\.TYPE_BYTE\b', 'gg.TYPE_BYTE'),
            (r'gg\.TYPE_DWORD\b', 'gg.TYPE_DWORD'),
            (r'gg\.TYPE_FLOAT\b', 'gg.TYPE_FLOAT'),
            (r'gg\.TYPE_DOUBLE\b', 'gg.TYPE_DOUBLE'),
            (r'gg\.TYPE_QWORD\b', 'gg.TYPE_QWORD'),
            (r'gg\.TYPE_WORD\b', 'gg.TYPE_WORD'),
            (r'gg\.TYPE_XOR\b', 'gg.TYPE_XOR'),
            (r'gg\.REGION_ANONYMOUS\b', 'gg.REGION_ANONYMOUS'),
            (r'gg\.REGION_CODE_APP\b', 'gg.REGION_CODE_APP'),
            (r'gg\.REGION_C_ALLOC\b', 'gg.REGION_C_ALLOC'),
            (r'gg\.REGION_C_HEAP\b', 'gg.REGION_C_HEAP'),
            (r'gg\.REGION_JAVA_HEAP\b', 'gg.REGION_JAVA_HEAP'),
            (r'gg\.REGION_OTHER\b', 'gg.REGION_OTHER'),
            (r'gg\.REGION_BAD\b', 'gg.REGION_BAD'),
            (r'gg\.REGION_STACK\b', 'gg.REGION_STACK'),
            (r'gg\.SIGN_EQUAL\b', 'gg.SIGN_EQUAL'),
            (r'gg\.SIGN_NOT_EQUAL\b', 'gg.SIGN_NOT_EQUAL'),
            (r'gg\.SIGN_GREATER\b', 'gg.SIGN_GREATER'),
            (r'gg\.SIGN_LESSER\b', 'gg.SIGN_LESSER'),
            (r'gg\.NUMBER_FLAG_FREEZE\b', 'gg.NUMBER_FLAG_FREEZE'),
            (r'gg\.NUMBER_FLAG_FROZEN\b', 'gg.NUMBER_FLAG_FROZEN'),
            (r'gg\.NUMBER_FLAG_NORMAL\b', 'gg.NUMBER_FLAG_NORMAL'),
            (r'gg\.NUMBER_FLAG_PAUSE\b', 'gg.NUMBER_FLAG_PAUSE'),
            (r'gg\.startFuzzy\s*\(\s*(.+?)\s*\)', r'gg.start_fuzzy(\1)'),
            (r'gg\.refineNumber\s*\(\s*"([^"]+)"\s*,\s*gg\.TYPE_(\w+)\s*(?:,\s*(.+?))?\s*\)', r'gg.refine_number("\1", gg.TYPE_\2, \3 or None)'),
        ]
        for pattern, repl in gg_patterns:
            regex = self.compile_regex(pattern)
            if callable(repl):
                code = regex.sub(repl, code)
            else:
                code = regex.sub(repl, code)
            if regex.search(code):
                self.gg_functions.add(pattern.split('.')[1].split('(')[0])

        return code

    def _string_char(self, args: str) -> str:
        tmp = self.new_temp_var()
        self.py_lines.append(f"{tmp} = ''.join(chr(int(x)) for x in ({args}))\n")
        return tmp

    def _table_sort(self, table: str, cmp: str = None) -> str:
        if cmp and 'function' in cmp:
            return f'{table}.sort(key=lambda a, b: {cmp})'
        elif cmp:
            return f'{table}.sort(key=lambda x: x.{cmp})'
        return f'{table}.sort()'

    def _handle_setmetatable(self, table: str, meta: str) -> str:
        self.metatables[table] = meta
        return f'__setmetatable({table}, {meta})'

    def _string_byte(self, s: str, start: str, end: str = None) -> str:
        if end:
            return f'[ord({s}[i]) for i in range(int({start})-1, min(int({end}), len({s})))]'
        return f'ord({s}[int({start})-1]) if len({s}) >= int({start}) else None'

    def _gg_search_number(self, value: str, type_: str, extra: str) -> str:
        self.gg_functions.add('searchNumber')
        mask = ''
        if extra:
            for flag in ['REGION_', 'SIGN_', 'NUMBER_FLAG_']:
                match = re.search(rf'gg\.({flag}(\w+))', extra)
                if match:
                    mask += f', {match.group(1).lower()}={match.group(1)}'
        return f'gg.search_number("{value}", gg.TYPE_{type_}{mask})'

    def _gg_search_fuzzy(self, value: str, type_: str, extra: str) -> str:
        self.gg_functions.add('searchFuzzy')
        return f'gg.search_fuzzy("{value}", gg.TYPE_{type_})'

    def _format_string(self, fmt: str, args: str) -> str:
        fmt = fmt.strip().strip('"\'')
        args = [a.strip() for a in args.split(',') if a.strip()]
        if not args:
            return f'"{fmt}"'
        placeholders = re.findall(r'\{(\d*)\}', fmt)
        if not placeholders:
            return f'"{fmt}" % ({", ".join(args)})'
        result = []
        last = 0
        for ph in placeholders:
            idx = int(ph) if ph else len(result)
            start = fmt.find('{' + ph + '}', last)
            result.append(fmt[last:start])
            result.append(f'{{{args[idx] if idx < len(args) else ""}}}')
            last = start + len(ph) + 2
        result.append(fmt[last:])
        return 'f"' + ''.join(result) + '"'

    def _handle_require(self, module: str) -> str:
        self.used_modules.add(module)
        return f'from {module} import *'

    def _handle_goto(self, label: str) -> str:
        self.goto_targets.append((self.current_line, label))
        return f'goto_{label}()'

    def _handle_label(self, label: str) -> str:
        self.label_map[label] = len(self.py_lines)
        return f'def goto_{label}(): pass'

    def parse_table(self, start: int, base_indent: int) -> int:
        i = start
        items: List[str] = []
        is_dict = False
        indent = base_indent + 4
        while i < len(self.lines):
            line = self.lines[i]
            stripped = line.strip()
            leading = len(line) - len(line.lstrip())
            if stripped == '}' and leading <= base_indent:
                break
            if stripped.startswith('{'):
                sub_start = i + 1
                i = self.parse_table(sub_start, leading)
                items.append('{...}')
                continue
            key_match = re.match(r'\[([^]=]+)\]\s*=\s*(.*)', stripped)
            if key_match:
                is_dict = True
                key, val = key_match.groups()
                val = val.rstrip(',').strip()
                items.append(f"{key}: {val}")
            elif '=' in stripped and not any(kw in stripped for kw in ['function', 'if', 'for', 'while', 'local', 'and', 'or']):
                is_dict = True
                parts = stripped.split('=', 1)
                key = parts[0].strip()
                val = parts[1].strip().rstrip(',')
                items.append(f"'{key}': {val}")
            else:
                val = stripped.rstrip(',')
                items.append(val)
            i += 1
        opener = '{' if is_dict else '['
        closer = '}' if is_dict else ']'
        self.py_lines.append(self.get_indent(base_indent) + opener + '\n')
        for item in items:
            self.py_lines.append(self.get_indent(indent) + item + ',\n')
        self.py_lines.append(self.get_indent(base_indent) + closer + '\n')
        return i

    def parse_block(self, start: int, indent: int, end_kw: str, structure_type: str) -> int:
        i = start
        while i < len(self.lines):
            line = self.lines[i]
            stripped = line.strip()
            leading = len(line) - len(line.lstrip())
            if stripped == end_kw and leading <= indent - 4:
                if self.stack and self.stack[-1]['type'] == structure_type:
                    self.stack.pop()
                return i + 1
            processed = self.apply_replacements(line)
            self.py_lines.append(self.get_indent(indent) + processed.rstrip() + '\n')
            i += 1
        self.warnings.append(f"Ожидался {end_kw} для {structure_type} (строка {start})")
        return i

    def convert(self) -> str:
        self.py_lines = []
        self.stack = []
        self.current_line = 0
        with self.scope_context():
            while self.current_line < len(self.lines):
                line = self.lines[self.current_line]
                stripped = line.strip()
                leading = len(line) - len(line.lstrip())

                if not stripped:
                    self.py_lines.append('\n')
                    self.current_line += 1
                    continue

                if stripped.startswith('--[['):
                    content, self.current_line = self.extract_multiline(self.current_line, True)
                    self.py_lines.append(f'# """{content}"""\n')
                    continue
                if stripped.startswith('[[') and not stripped.startswith('--[['):
                    content, self.current_line = self.extract_multiline(self.current_line, False)
                    self.py_lines.append(f'"""{content}"""\n')
                    continue
                if stripped.startswith('--'):
                    self.py_lines.append(re.sub(r'^--', '#', line).rstrip() + '\n')
                    self.current_line += 1
                    continue

                if re.match(r'^\s*[\w_]+\s*=\s*\{', line):
                    var_match = re.match(r'^(\s*)([\w_]+)\s*=\s*\{', line)
                    if var_match:
                        indent_str, var = var_match.groups()
                        self.py_lines.append(f"{indent_str}{var} = " + '\n')
                    self.current_line = self.parse_table(self.current_line + 1, leading)
                    continue

                func_match = re.match(r'^(\s*)(local\s+)?function\s+(\w+)\s*\(([^)]*)\)', line)
                if func_match:
                    indent_str, local, name, args = func_match.groups()
                    args = re.sub(r'\.\.\.', '*args', args)
                    self.py_lines.append(f"{indent_str}def {name}({args}):\n")
                    self.stack.append({'type': 'function', 'indent': leading, 'line': self.current_line})
                    self.function_depth += 1
                    with self.scope_context():
                        self.current_line = self.parse_block(self.current_line + 1, leading + 4, 'end', 'function')
                    self.function_depth -= 1
                    continue

                if_match = re.match(r'^(\s*)if\s+(.+?)\s+then\s*$', line)
                if if_match:
                    indent_str, cond = if_match.groups()
                    self.py_lines.append(f"{indent_str}if {cond}:\n")
                    self.stack.append({'type': 'if', 'indent': leading, 'line': self.current_line})
                    self.current_line += 1
                    continue

                elif_match = re.match(r'^(\s*)elseif\s+(.+?)\s+then\s*$', line)
                if elif_match and self.stack and self.stack[-1]['type'] == 'if':
                    indent_str, cond = elif_match.groups()
                    self.py_lines.append(f"{indent_str}elif {cond}:\n")
                    self.current_line += 1
                    continue

                if stripped == 'else' and self.stack and self.stack[-1]['type'] == 'if':
                    self.py_lines.append(f"{self.get_indent(leading)}else:\n")
                    self.current_line += 1
                    continue

                if stripped == 'end' and self.stack and self.stack[-1]['indent'] == leading:
                    self.stack.pop()
                    self.current_line += 1
                    continue

                while_match = re.match(r'^(\s*)while\s+(.+?)\s+do\s*$', line)
                if while_match:
                    indent_str, cond = while_match.groups()
                    self.py_lines.append(f"{indent_str}while {cond}:\n")
                    self.stack.append({'type': 'while', 'indent': leading})
                    self.current_line += 1
                    continue

                for_num_match = re.match(r'^(\s*)for\s+(\w+)\s*=\s*(.+?)\s*,\s*(.+?)(?:\s*,\s*(.+?))?\s+do\s*$', line)
                if for_num_match:
                    indent_str, var, start, stop, step = for_num_match.groups()
                    step = step or '1'
                    self.py_lines.append(f"{indent_str}for {var} in range(int({start}), int({stop}) + 1, int({step})):\n")
                    self.stack.append({'type': 'for', 'indent': leading})
                    self.current_line += 1
                    continue

                for_gen_match = re.match(r'^(\s*)for\s+(.+?)\s+in\s+(.+?)\s+do\s*$', line)
                if for_gen_match:
                    indent_str, vars_part, iter_part = for_gen_match.groups()
                    self.py_lines.append(f"{indent_str}for {vars_part} in {iter_part}:\n")
                    self.stack.append({'type': 'for', 'indent': leading})
                    self.current_line += 1
                    continue

                if stripped == 'repeat':
                    self.py_lines.append(f"{self.get_indent(leading)}while True:\n")
                    self.stack.append({'type': 'repeat', 'indent': leading})
                    self.current_line += 1
                    continue

                until_match = re.match(r'^(\s*)until\s+(.+)$', line)
                if until_match and self.stack and self.stack[-1]['type'] == 'repeat':
                    indent_str, cond = until_match.groups()
                    self.py_lines.append(f"{indent_str}    if not ({cond}): break\n")
                    self.stack.pop()
                    self.current_line += 1
                    continue

                if stripped.startswith('local '):
                    local_vars = re.findall(r'local\s+([\w,\s]+?)\s*=', line) or re.findall(r'local\s+([\w,\s]+)', line)
                    for var in local_vars:
                        for v in [x.strip() for x in var.split(',')]:
                            self.scopes[-1].locals.add(v)
                    line = re.sub(r'^(\s*)local\s+', r'\1', line)

                if stripped.startswith('return '):
                    returns = [r.strip() for r in stripped[7:].split(',')]
                    if len(returns) > 1:
                        line = line.replace(stripped, f"return ({', '.join(returns)})")

                line = self.apply_replacements(line)
                self.py_lines.append(line.rstrip() + '\n')
                self.current_line += 1

        if self.stack:
            for s in self.stack:
                self.warnings.append(f"Не закрыта структура {s['type']} (строка {s['line'] + 1})")

        py_code = ''.join(self.py_lines)

        if any(x in py_code for x in ['time.', 'sleep', 'strftime', 'mktime', 'perf_counter']): self.add_import('import time')
        if 'random.' in py_code: self.add_import('import random')
        if 'threading.' in py_code: self.add_import('import threading')
        if any(x in py_code for x in ['math.', 'floor', 'sin', 'pi', 'radians', 'inf']): self.add_import('import math')
        if 're.' in py_code: self.add_import('import re')
        if 'gc.' in py_code: self.add_import('import gc')
        if 'traceback.' in py_code: self.add_import('import traceback')
        if any(x in py_code for x in ['f"', '{', 'Dict', 'List', 'struct_time', 'Generator']): self.add_import('from typing import Dict, List, Any, Tuple, Generator')
        if '__pcall_wrapper' in py_code or '__assert_wrapper' in py_code or '__xpcall_wrapper' in py_code:
            self.add_import('def __pcall_wrapper(func): try: return func() except Exception as e: return False, str(e)')
            self.add_import('def __xpcall_wrapper(func, err): try: return func() except Exception as e: err(e); return False')
            self.add_import('def __assert_wrapper(cond): if not cond: raise AssertionError(cond)')
            self.add_import('def __getupvalue(f, i): return f.__closure__[i].cell_contents if f.__closure__ and i < len(f.__closure__) else None')
            self.add_import('def __setupvalue(f, i, v): if f.__closure__ and i < len(f.__closure__): f.__closure__[i].cell_contents = v')
            self.add_import('def __yield(*args): return args')
            self.add_import('def __coroutine_wrap(f): def wrapper(*a, **k): return f(*a, **k); yield; return wrapper')
        if self.gg_functions:
            self.add_import('import gg')
        if self.roblox_functions:
            self.add_import('from enum import Enum')

        header = '\n'.join(sorted(self.imports)) + '\n\n' if self.imports else ''
        warnings_block = '\n'.join(['# Предупреждения:'] + self.warnings + ['\n']) if self.warnings else ''

        return warnings_block + header + py_code


def main():
    print("LuaEndToPy — Конвертер Lua в Python")
    input_path = input("Путь к .lua файлу: ").strip().strip('"\'')
    output_path = input("Путь для .py файла: ").strip().strip('"\'')
    if not os.path.isfile(input_path):
        print("Файл не найден.")
        return
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    parser = LuaEndToPy()
    parser.load_file(input_path)
    py_code = parser.convert()
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(py_code)
    print(f"Конвертация завершена: {output_path}")
    if parser.warnings:
        print(f"Предупреждений: {len(parser.warnings)}")


if __name__ == '__main__':
    main()
