/*
server/panic_guard.go
后台协程的 panic 兜底

Go 里未捕获的 goroutine panic 会让**整个进程退出**，net/http 只兜得住请求处理协程。
这个平台的后台协程不少（代理抓源、测通、测速、后台刷新、Cookie 检测），任何一处的
nil 指针或越界都能把服务打挂 —— 而平台一挂，所有在跑的签到 job 的预取、凭据回写、
run-state 上报会同时断掉，代价远大于「这一个后台任务失败」。

所以后台协程统一在最外层挂一个 recover：记下标签与堆栈，让这一个任务失败，进程继续活。
注意不要用它包住需要 WaitGroup 配合的清理逻辑 —— defer 是 LIFO，把 recover 放在
wg.Done() 之后注册就行，panic 时两者都会执行。
*/
package main

import (
	"fmt"
	"log"
	"runtime/debug"
)

// recoverPanic 兜住后台协程的 panic 并记录堆栈。用法：defer recoverPanic("标签")。
func recoverPanic(label string) {
	if v := recover(); v != nil {
		log.Printf("[panic] %s: %v\n%s", label, v, debug.Stack())
	}
}

/*
recoverPanicWith 兜住 panic 并把原因交给回调，用于需要额外收尾的场景。

比如 Cookie 检测任务：光记日志不够，还得把 running 置回去、把原因写进 last_error，
否则 IsRunning() 会一直是 true，之后所有检测请求都被 409 挡掉，只能重启服务。
*/
func recoverPanicWith(label string, onPanic func(reason string)) {
	if v := recover(); v != nil {
		log.Printf("[panic] %s: %v\n%s", label, v, debug.Stack())
		if onPanic != nil {
			onPanic(fmt.Sprintf("%v", v))
		}
	}
}
