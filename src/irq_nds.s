// SPDX-License-Identifier: MIT
// IRQ entry hook and panic mode switch for the DSi agent.
    .syntax unified
    .cpu    arm946e-s
    .arm

    .section .text.dsidbg_irq, "ax", %progbits
    .align  2

// Installed as the BIOS IRQ handler, in front of libnds' IntrMain. The BIOS has pushed {r0-r3, r12, lr} on the IRQ
// stack; the saved lr is the interrupted instruction + 4. r0-r3 and r12 are free to use, lr must be kept.
    .global dsidbg__irq_entry
    .type   dsidbg__irq_entry, %function
dsidbg__irq_entry:
    mov     r3, sp                  // r3 = BIOS frame
    bic     sp, sp, #7              // AAPCS stack alignment for the C hook
    push    {r3, lr}
    ldr     r0, [r3, #20]
    sub     r0, r0, #4              // r0 = interrupted pc
    mov     r12, #0x04000000
    ldr     r1, [r12, #0x210]       // IE
    ldr     r2, [r12, #0x214]       // IF
    and     r1, r1, r2              // r1 = pending
    ldr     r12, =dsidbg__irq_hook
    blx     r12
    pop     {r3, lr}
    mov     sp, r3
    cmp     r0, #0
    bne     .Lhang
    ldr     r12, =dsidbg__irq_next
    ldr     pc, [r12]

// Hang: save the interrupted registers into dsidbg__crash.regs and enter the panic loop. r4-r11 still hold the
// interrupted values (the C hook preserved them).
.Lhang:
    ldr     r12, =dsidbg__crash
    add     r12, r12, #4            // &regs[0]
    add     r0, r12, #16
    stmia   r0, {r4-r11}            // regs[4..11]
    ldmia   sp, {r0-r3}
    stmia   r12, {r0-r3}            // regs[0..3]
    ldr     r0, [sp, #16]
    str     r0, [r12, #48]          // regs[12]
    ldr     r0, [sp, #20]
    sub     r0, r0, #4
    str     r0, [r12, #60]          // regs[15] = pc
    mrs     r1, spsr
    str     r1, [r12, #64]          // cpsr of the interrupted code
    mrs     r2, cpsr                // read the interrupted mode's banked sp/lr (user mode -> system mode)
    and     r3, r1, #0x1F
    cmp     r3, #0x10
    moveq   r3, #0x1F
    bic     r0, r2, #0x1F
    orr     r0, r0, r3
    msr     cpsr_c, r0
    mov     r0, sp
    mov     r1, lr
    msr     cpsr_c, r2
    str     r0, [r12, #52]          // regs[13]
    str     r1, [r12, #56]          // regs[14]
    b       dsidbg__panic_switch

// Leaves the exception/IRQ mode for good: system mode, interrupts on, own stack, then the C panic loop.
    .global dsidbg__panic_switch
    .type   dsidbg__panic_switch, %function
dsidbg__panic_switch:
    mrs     r0, cpsr
    bic     r0, r0, #0xDF           // clear mode, I and F
    orr     r0, r0, #0x1F           // system mode
    msr     cpsr_c, r0
    ldr     sp, =dsidbg__panic_stack_top
    ldr     r12, =dsidbg__panic_main
    blx     r12
1:  b       1b

    .ltorg

    .section .bss.dsidbg_panic, "aw", %nobits
    .align  3
dsidbg__panic_stack:
    .space  8192
dsidbg__panic_stack_top:
