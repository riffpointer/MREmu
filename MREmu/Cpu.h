#pragma once
#include <cstdint>

namespace Cpu {
	void init();
	extern uint64_t g_cpu_instructions_executed;
	void imgui_REG();
	void trace_on();
};