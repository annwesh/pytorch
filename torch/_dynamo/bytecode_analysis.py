import bisect
import dataclasses
import dis
import sys
from collections import deque
from numbers import Real

TERMINAL_OPCODES = {
    dis.opmap["RETURN_VALUE"],
    dis.opmap["JUMP_FORWARD"],
    dis.opmap["RAISE_VARARGS"],
    # TODO(jansel): double check exception handling
}
if sys.version_info >= (3, 9):
    TERMINAL_OPCODES.add(dis.opmap["RERAISE"])
if sys.version_info >= (3, 11):
    TERMINAL_OPCODES.add(dis.opmap["JUMP_BACKWARD"])
    TERMINAL_OPCODES.add(dis.opmap["JUMP_BACKWARD_NO_INTERRUPT"])
else:
    TERMINAL_OPCODES.add(dis.opmap["JUMP_ABSOLUTE"])
JUMP_OPCODES = set(dis.hasjrel + dis.hasjabs)
JUMP_OPNAMES = {dis.opname[opcode] for opcode in JUMP_OPCODES}
HASLOCAL = set(dis.haslocal)
HASFREE = set(dis.hasfree)

stack_effect = dis.stack_effect


def get_indexof(insts):
    """
    Get a mapping from instruction memory address to index in instruction list.
    Additionally checks that each instruction only appears once in the list.
    """
    indexof = {}
    for i, inst in enumerate(insts):
        assert inst not in indexof
        indexof[inst] = i
    return indexof


def remove_dead_code(instructions):
    """Dead code elimination"""
    indexof = get_indexof(instructions)
    live_code = set()

    def find_live_code(start):
        for i in range(start, len(instructions)):
            if i in live_code:
                return
            live_code.add(i)
            inst = instructions[i]
            if inst.exn_tab_entry:
                find_live_code(indexof[inst.exn_tab_entry.target])
            if inst.opcode in JUMP_OPCODES:
                find_live_code(indexof[inst.target])
            if inst.opcode in TERMINAL_OPCODES:
                return

    find_live_code(0)

    # change exception table entries if start/end instructions are dead
    # assumes that exception table entries have been propagated,
    # e.g. with bytecode_transformation.propagate_inst_exn_table_entries,
    # and that instructions with an exn_tab_entry lies within its start/end.
    if sys.version_info >= (3, 11):
        live_idx = sorted(live_code)
        for i, inst in enumerate(instructions):
            if i in live_code and inst.exn_tab_entry:
                # find leftmost live instruction >= start
                start_idx = bisect.bisect_left(
                    live_idx, indexof[inst.exn_tab_entry.start]
                )
                assert start_idx < len(live_idx)
                # find rightmost live instruction <= end
                end_idx = (
                    bisect.bisect_right(live_idx, indexof[inst.exn_tab_entry.end]) - 1
                )
                assert end_idx >= 0
                assert live_idx[start_idx] <= i <= live_idx[end_idx]
                inst.exn_tab_entry.start = instructions[live_idx[start_idx]]
                inst.exn_tab_entry.end = instructions[live_idx[end_idx]]

    return [inst for i, inst in enumerate(instructions) if i in live_code]


def remove_pointless_jumps(instructions):
    """Eliminate jumps to the next instruction"""
    pointless_jumps = {
        id(a)
        for a, b in zip(instructions, instructions[1:])
        if a.opname == "JUMP_ABSOLUTE" and a.target is b
    }
    return [inst for inst in instructions if id(inst) not in pointless_jumps]


def propagate_line_nums(instructions):
    """Ensure every instruction has line number set in case some are removed"""
    cur_line_no = None

    def populate_line_num(inst):
        nonlocal cur_line_no
        if inst.starts_line:
            cur_line_no = inst.starts_line

        inst.starts_line = cur_line_no

    for inst in instructions:
        populate_line_num(inst)


def remove_extra_line_nums(instructions):
    """Remove extra starts line properties before packing bytecode"""

    cur_line_no = None

    def remove_line_num(inst):
        nonlocal cur_line_no
        if inst.starts_line is None:
            return
        elif inst.starts_line == cur_line_no:
            inst.starts_line = None
        else:
            cur_line_no = inst.starts_line

    for inst in instructions:
        remove_line_num(inst)


@dataclasses.dataclass
class ReadsWrites:
    reads: set
    writes: set
    visited: set


def livevars_analysis(instructions, instruction):
    indexof = get_indexof(instructions)
    must = ReadsWrites(set(), set(), set())
    may = ReadsWrites(set(), set(), set())

    def walk(state, start):
        if start in state.visited:
            return
        state.visited.add(start)

        for i in range(start, len(instructions)):
            inst = instructions[i]
            if inst.opcode in HASLOCAL or inst.opcode in HASFREE:
                if "LOAD" in inst.opname or "DELETE" in inst.opname:
                    if inst.argval not in must.writes:
                        state.reads.add(inst.argval)
                elif "STORE" in inst.opname:
                    state.writes.add(inst.argval)
                elif inst.opname == "MAKE_CELL":
                    pass
                else:
                    raise NotImplementedError(f"unhandled {inst.opname}")
            if inst.opcode in JUMP_OPCODES:
                walk(may, indexof[inst.target])
                state = may
            if inst.opcode in TERMINAL_OPCODES:
                return

    walk(must, indexof[instruction])
    return must.reads | may.reads


@dataclasses.dataclass
class StackSize:
    low: Real
    high: Real

    def zero(self):
        self.low = 0
        self.high = 0

    def offset_of(self, other, n):
        prior = (self.low, self.high)
        self.low = min(self.low, other.low + n)
        self.high = max(self.high, other.high + n)
        if (self.low, self.high) != prior:
            return True
        else:
            return False


def stacksize_analysis(instructions):
    assert instructions
    print("Enter stacksize_analysis -------------------------- ")
    stack_sizes = {
        inst: StackSize(float("inf"), float("-inf")) for inst in instructions
    }
    stack_sizes[instructions[0]].zero()

    indexof = {id(inst): i for i, inst in enumerate(instructions)}

    worklist = deque()
    worklist.append(0)
    i = 0

    while len(worklist) != 0:
        i += 1
        if i > 20:
            print("^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^")
            import traceback
            traceback.print_stack()
            print("^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^")
        
        index = worklist.popleft()
        inst = instructions[index]
        stack_size = stack_sizes[inst]
        if inst.opcode not in TERMINAL_OPCODES:
            assert index + 1 < len(instructions), f"missing next inst: {inst}"
            changed = stack_sizes[instructions[index + 1]].offset_of(
                stack_size, stack_effect(inst.opcode, inst.arg, jump=False)
            )
            if changed:
                worklist.append(index + 1)
        if inst.opcode in JUMP_OPCODES:
            changed = stack_sizes[inst.target].offset_of(
                stack_size, stack_effect(inst.opcode, inst.arg, jump=True)
            )
            if changed:
                worklist.append(indexof[id(inst.target)])

    if False:
        for inst in instructions:
            stack_size = stack_sizes[inst]
            print(stack_size.low, stack_size.high, inst)

    low = min([x.low for x in stack_sizes.values()])
    high = max([x.high for x in stack_sizes.values()])
    print("Leave stacksize_analysis -------------------------- ")
    assert low >= 0
    return high
