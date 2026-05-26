runToolUse
    -> streamedCheckPermissionsAndCallTool -> checkPermissionsAndCallTool ->  
    processToolResultBlock ： tool结果太大，会将结果放到文件，避免直接输出，只返回前面的结果

microcompact:
    1.maybeTimeBasedMicrocompact:基于时间进行过期处理，服务端的prompt cache是有时间的，超过时间即使前缀相同，也不能命中cache
        1.比较上一次消息的时间是否超过了缓存的时间
        2.收集可压缩的工具列表
        3.只保留最近n个工具结果
        4.其余的工具替换为'[Old tool result content cleared]'
        可压缩工具列表
        const COMPACTABLE_TOOLS = new Set([
            FILE_READ_TOOL_NAME,    // 读文件 → 可以重新读
            ...SHELL_TOOL_NAMES,    // 执行命令 → 可以重新执行
            GREP_TOOL_NAME,         // 搜索 → 可以重新搜
            GLOB_TOOL_NAME,         // 查找文件 → 可以重新查
            WEB_SEARCH_TOOL_NAME,   // 搜索网页 → 可以重新搜
            FILE_EDIT_TOOL_NAME,    // 编辑文件 → 结果可裁剪
            FILE_WRITE_TOOL_NAME,   // 写文件 → 结果可裁剪
        ])  
    2.cached microcompact 直接调用Anthropic 的cache_edits API，在不重写缓存前缀的情况下删除工具结果

autoCompactIfNeeded
    trySessionMemoryCompaction 
        处理以下两种情况：
            1.lastSummarizedMessageId 存在，那么只保存在此id之后的消息
            2.对于resume的session。lastSummarizedMessageId不存在，session memory存在，返回session memory
        waitForSessionMemoryExtraction ：等待当前还在提取session memory的线程节俗
        const lastSummarizedMessageId = getLastSummarizedMessageId() // 获取上一次总结的消息id
        const sessionMemory = await getSessionMemoryContent()  // 从文件{projectDir}/{sessionId}/session-memory/summary.md中获取session memory
        根据lastSummarizedMessageId 去messages中查找对应的消息idx
        calculateMessagesToKeepIndex 计算从哪一条id开始保留messages
            calculateMessagesToKeepIndex 之后的所有消息都要无条件保留
            然后calculateMessagesToKeepIndex之前的一条条扩充，知道总token >= maxTokens(40k)或者总token >= minTokens(10k) && Text消息数 >= minTextBlockMessages(5)
            adjustIndexToPreserveAPIInvariants 保证消息的完整性，必须保证(thinking -> tool_use -> tool_result)的完整性
                thinking和tool_use 之间message.id是相同的
                tool_use和tool_result 之间block.id是相同的
        createCompactionResultFromSessionMemory
            truncateSessionMemoryForCompact(sessionMemory)
                截断每个section的内容(md风格，section按照# 划分) 每个section最多8000
                如果有section被截断需要在消息中添加
                    Some session memory sections were truncated for length. The full session memory can be viewed at: ${memoryPath}
            getCompactUserSummaryMessage 添加提示词
    compactConversation
        通过一次模型调用来compact。如果当前的messages太大，需要进行truncate，然后在调用模型
sessionMemory 创建流程
    1. 初始化的时候在setup.js initSessionMemory()将extractSessionMemory 注册到postSamplingHooks
    2. queryloop，每轮对话结束调用executePostSamplingHooks -> 将extractSessionMemory
    3. 当前对话满足一定条件，调用模型将对话compact 写入到summary.md中

Context Anxiety
    模型上下文快要耗尽时，会过早的收尾工作
自我评估偏差
    模型过于自信的认同自己产生的结果
 
memory系统
    getSystemPrompt -> loadMemoryPrompt() -> buildMemoryLines
        定义memory文件怎么存，存储的文件模板
        定义四种文件类型
            user：
            feedback
            project
            reference
        定义哪些内容不用存
auto 模式：queryloop -> handleStopHooks -> executeExtractMemories  -> runExtraction  

getRelevantMemoryAttachments 是一个异步操作，
只是在获取完成后才假如到对话上下文中。避免因为获取相关memory导致对话卡住，这也说明memory其实不是一个必须的信息，因为从上下文也可以计算出对应的信息
