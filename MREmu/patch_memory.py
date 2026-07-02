import re

with open('Memory.cpp', 'r') as f:
    content = f.read()

# malloc
content = re.sub(
    r'for \(int i = 0; i < regions\.size\(\); \+\+i\) \{[\s\S]*?new_adr = regions\[i\]\.adr \+ regions\[i\]\.size;\n\t\t\}',
    r'''for (auto it = regions.begin(); it != regions.end(); ++it) {
			if (new_adr % align != 0)
				new_adr = ((new_adr / align) + 1) * align;

			if (new_adr + size <= it->adr) {
				regions.insert(it, { new_adr, size });
				free_memory_size -= size;
				return new_adr;
			}

			new_adr = it->adr + it->size;
		}''', content)

# realloc
realloc_orig = r'''		int mem_ind = -1;
		for \(int i = 0; i < regions\.size\(\); \+\+i\) \{
			if \(regions\[i\]\.adr == addr\) \{
				mem_ind = i;
				break;
			\}
		\}

		if\(mem_ind == -1\)
			return malloc\(size\);

		if \(size <= regions\[mem_ind\]\.size\) \{
			free_memory_size \+= regions\[mem_ind\]\.size - size;
			regions\[mem_ind\]\.size = size;
			return regions\[mem_ind\]\.adr;
		\}

		size_t allow_max_size = mem_size - \(regions\[mem_ind\]\.adr - start_adr\);
		if \(mem_ind \+ 1 < regions\.size\(\)\)
			allow_max_size = regions\[mem_ind \+ 1\]\.adr - regions\[mem_ind\]\.adr;

		if \(allow_max_size >= size\) \{
			free_memory_size -= size - regions\[mem_ind\]\.size;
			regions\[mem_ind\]\.size = size;
			return regions\[mem_ind\]\.adr;
		\}

		size_t new_adr = malloc\(size\);

		if \(new_adr == 0\)
			return 0;

		memcpy\(\(void\*\)new_adr, \(void\*\)regions\[mem_ind\]\.adr, regions\[mem_ind\]\.size\); //need to be careful
		free\(regions\[mem_ind\]\.adr\);'''

realloc_new = r'''		auto it = regions.begin();
		for (; it != regions.end(); ++it) {
			if (it->adr == addr) {
				break;
			}
		}

		if (it == regions.end())
			return malloc(size);

		if (size <= it->size) {
			free_memory_size += it->size - size;
			it->size = size;
			return it->adr;
		}

		size_t allow_max_size = mem_size - (it->adr - start_adr);
		auto next_it = it;
		++next_it;
		if (next_it != regions.end())
			allow_max_size = next_it->adr - it->adr;

		if (allow_max_size >= size) {
			free_memory_size -= size - it->size;
			it->size = size;
			return it->adr;
		}

		size_t new_adr = malloc(size);

		if (new_adr == 0)
			return 0;

		memcpy((void*)new_adr, (void*)it->adr, it->size);
		free(it->adr);'''

content = re.sub(realloc_orig, realloc_new, content)

# free
free_orig = r'''		for \(int i = 0; i < regions\.size\(\); \+\+i\) \{
			if \(regions\[i\]\.adr == addr\) \{
				free_memory_size \+= regions\[i\]\.size;
				regions\.erase\(regions\.begin\(\) \+ i\);
				return;
			\}
		\}'''

free_new = r'''		for (auto it = regions.begin(); it != regions.end(); ++it) {
			if (it->adr == addr) {
				free_memory_size += it->size;
				regions.erase(it);
				return;
			}
		}'''

content = re.sub(free_orig, free_new, content)

with open('Memory.cpp', 'w') as f:
    f.write(content)
