"""The enhanced renderer — a VM-independent *presentation* layer that consumes the verified
render model (GameFrameSnapshot / FrameCapture) and the faithful RGB frames, and draws on its
own (higher) clock with inter-frame interpolation.

Unlike the faithful renderer (EGA planes, byte-diffed vs the ASM), this layer has no byte-exact
obligation: it is a modern presentation grounded in the byte-verified model. v1 = scroll-motion
interpolation of the faithful frames (smooth scrolling); the HUD stays fixed. Non-gameplay scenes
(intro/menu/map), which the model does not yet describe, fall back to the plain faithful frame.
"""
