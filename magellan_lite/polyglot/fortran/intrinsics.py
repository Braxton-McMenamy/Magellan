"""Names that are Fortran intrinsics (standard, F77 specific names, common GNU extensions).

A name here that the unit does not declare ``EXTERNAL`` is the intrinsic, not a
project procedure, which is the language rule. Keep it generous: a missing entry
turns an intrinsic into a low-confidence edge to an external, never a wrong edge
into the project (unless the project defines a procedure of the same name).
"""

INTRINSICS = frozenset("""
abs achar acos acosh adjustl adjustr aimag aint all allocated alog alog10 amax0 amax1 amin0
amin1 amod anint any asin asinh associated atan atan2 atanh bessel_j0 bessel_j1 bessel_jn
bessel_y0 bessel_y1 bessel_yn bge bgt bit_size ble blt btest cabs ccos ceiling cexp char clog
cmplx command_argument_count conjg cos cosh count cpu_time cshift csin csqrt dabs dacos dasin
datan datan2 date_and_time dble dcmplx dconjg dcos dcosh ddim dexp dfloat digits dim dimag
dint dlog dlog10 dmax1 dmin1 dmod dnint dot_product dprod dreal dshiftl dshiftr dsign dsin
dsinh dsqrt dtan dtanh eoshift epsilon erf erfc erfc_scaled execute_command_line exp exponent
extends_type_of findloc float floor fraction gamma get_command get_command_argument
get_environment_variable huge hypot iabs iachar iall iand iany ibclr ibits ibset ichar idim
idint idnint ieor ifix image_index index int ior iparity is_contiguous is_iostat_end
is_iostat_eor ishft ishftc isign kind lbound lcobound leadz len len_trim lge lgt lle llt log
log10 log_gamma logical maskl maskr matmul max max0 max1 maxexponent maxloc maxval merge
merge_bits min min0 min1 minexponent minloc minval mod modulo move_alloc mvbits nearest
new_line nint norm2 not null num_images pack parity popcnt poppar precision present product
radix random_number random_seed range rank real repeat reshape rrspacing same_type_as scale
scan selected_char_kind selected_int_kind selected_real_kind set_exponent shape shifta
shiftl shiftr sign sin sinh size sngl spacing spread sqrt storage_size sum system_clock tan
tanh this_image tiny trailz transfer transpose trim ubound ucobound unpack verify
c_loc c_funloc c_f_pointer c_f_procpointer c_associated c_sizeof
ieee_is_nan ieee_is_finite ieee_value ieee_class ieee_support_nan ieee_is_normal
ieee_get_flag ieee_set_flag ieee_get_halting_mode ieee_set_halting_mode
ieee_get_rounding_mode ieee_set_rounding_mode compiler_version compiler_options
omp_get_thread_num omp_get_num_threads omp_get_max_threads omp_get_wtime omp_set_num_threads
omp_in_parallel omp_get_num_procs
co_sum co_min co_max co_broadcast co_reduce atomic_define atomic_ref atomic_add atomic_and
atomic_or atomic_xor atomic_cas atomic_fetch_add atomic_fetch_and atomic_fetch_or
atomic_fetch_xor event_query image_status failed_images stopped_images get_team team_number
coshape lcobound ucobound out_of_range reduce split tokenize
abort and access alarm besj0 besj1 besjn besy0 besy1 besyn chdir chmod complex ctime dbesj0
dbesj1 dbesjn dbesy0 dbesy1 dbesyn derf derfc dtime etime exit fdate fget fgetc flush
fnum fput fputc free fseek fstat ftell gerror getarg getcwd getenv getgid getlog getpid
getuid gmtime hostnm iargc idate ierrno imag imagpart irand isatty isnan itime kill lnblnk
loc lshift lstat ltime malloc mclock mclock8 or perror qext rand realpart rename rshift
second secnds signal sizeof sleep srand stat symlnk system time time8 ttynam umask unlink xor
zabs zcos zexp zlog zsin zsqrt cdabs cdexp cdlog cdsqrt cdsin cdcos
""".split())

#: Intrinsic *subroutines*. ``CALL`` of any other intrinsic's name (``CALL SCALE(X)``) is a
#: call to the project's (or an external) subroutine of that name: an intrinsic function
#: cannot be CALLed, and gfortran resolves it to the external.
INTRINSIC_SUBROUTINES = frozenset("""
cpu_time date_and_time system_clock random_number random_seed mvbits move_alloc get_command
get_command_argument get_environment_variable execute_command_line c_f_pointer
c_f_procpointer ieee_get_flag ieee_set_flag ieee_get_halting_mode ieee_set_halting_mode
ieee_get_rounding_mode ieee_set_rounding_mode omp_set_num_threads
co_sum co_min co_max co_broadcast co_reduce atomic_define atomic_ref atomic_add atomic_and
atomic_or atomic_xor atomic_cas atomic_fetch_add atomic_fetch_and atomic_fetch_or
atomic_fetch_xor event_query split tokenize
abort alarm chdir chmod ctime dtime etime exit fdate fget fgetc flush fput fputc free fseek
fstat gerror getarg getcwd getenv getlog gmtime hostnm idate itime kill link lstat ltime
perror rename second signal sleep srand stat symlnk system time ttynam umask unlink
""".split())

#: Intrinsic modules: a USE of one of these does not make a scope's names unknowable.
INTRINSIC_MODULES = frozenset({
    "iso_c_binding", "iso_fortran_env", "ieee_arithmetic", "ieee_exceptions",
    "ieee_features", "omp_lib", "omp_lib_kinds", "openacc",
})
