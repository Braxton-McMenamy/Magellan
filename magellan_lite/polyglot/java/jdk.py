"""The little the Java frontend needs to know about the JDK, without a JDK.

Names that resolve without an import (``java.lang``), the common members of the
packages most code imports on demand (``java.util.*``, ``java.io.*``), which
exceptions are unchecked, and which calls touch the outside world. Anything not
listed here resolves to a bare external name, which is still an endpoint.
"""

from __future__ import annotations

JAVA_LANG = frozenset("""
AbstractMethodError Appendable ArithmeticException ArrayIndexOutOfBoundsException
ArrayStoreException AssertionError AutoCloseable Boolean BootstrapMethodError Byte Character
CharSequence Class ClassCastException ClassCircularityError ClassFormatError ClassLoader
ClassNotFoundException ClassValue CloneNotSupportedException Cloneable Comparable Deprecated
Double Enum EnumConstantNotPresentException Error Exception ExceptionInInitializerError Float
FunctionalInterface IllegalAccessError IllegalAccessException IllegalArgumentException
IllegalCallerException IllegalMonitorStateException IllegalStateException
IllegalThreadStateException IncompatibleClassChangeError IndexOutOfBoundsException
InheritableThreadLocal InstantiationError InstantiationException Integer InternalError
InterruptedException Iterable LayerInstantiationException LinkageError Long MatchException Math
Module ModuleLayer NegativeArraySizeException NoClassDefFoundError NoSuchFieldError
NoSuchFieldException NoSuchMethodError NoSuchMethodException NullPointerException Number
NumberFormatException Object OutOfMemoryError Override Package Process ProcessBuilder
ProcessHandle Readable Record ReflectiveOperationException Runnable Runtime RuntimeException
SafeVarargs ScopedValue SecurityException SecurityManager Short StackOverflowError
StackTraceElement StackWalker StrictMath String StringBuffer StringBuilder
StringIndexOutOfBoundsException SuppressWarnings System Thread ThreadDeath ThreadGroup
ThreadLocal Throwable TypeNotPresentException UnknownError UnsatisfiedLinkError
UnsupportedClassVersionError UnsupportedOperationException VerifyError VirtualMachineError Void
""".split())

JAVA_UTIL = frozenset("""
AbstractCollection AbstractList AbstractMap AbstractQueue AbstractSequentialList AbstractSet
ArrayDeque ArrayList Arrays Base64 BitSet Calendar Collection Collections Comparator
ConcurrentModificationException Currency Date Deque Dictionary DoubleSummaryStatistics
EmptyStackException EnumMap EnumSet Enumeration EventListener EventObject Formatter GregorianCalendar
HashMap HashSet Hashtable IdentityHashMap IllegalFormatException InputMismatchException
IntSummaryStatistics Iterator LinkedHashMap LinkedHashSet LinkedList List ListIterator Locale Map
MissingResourceException NavigableMap NavigableSet NoSuchElementException Objects Observable
Observer Optional OptionalDouble OptionalInt OptionalLong PriorityQueue Properties Queue Random
RandomAccess ResourceBundle Scanner SequencedCollection SequencedMap SequencedSet ServiceLoader Set
SortedMap SortedSet Spliterator Spliterators SplittableRandom Stack StringJoiner StringTokenizer
Timer TimerTask TimeZone TreeMap TreeSet UUID Vector WeakHashMap
""".split())

JAVA_IO = frozenset("""
BufferedInputStream BufferedOutputStream BufferedReader BufferedWriter ByteArrayInputStream
ByteArrayOutputStream CharArrayReader CharArrayWriter Closeable Console DataInput DataInputStream
DataOutput DataOutputStream EOFException Externalizable File FileDescriptor FileFilter
FileInputStream FileNotFoundException FileOutputStream FileReader FileWriter FilenameFilter
FilterInputStream FilterOutputStream FilterReader FilterWriter Flushable IOError IOException
InputStream InputStreamReader InterruptedIOException InvalidClassException InvalidObjectException
LineNumberReader NotSerializableException ObjectInput ObjectInputStream ObjectOutput
ObjectOutputStream ObjectStreamException OutputStream OutputStreamWriter PipedInputStream
PipedOutputStream PrintStream PrintWriter PushbackInputStream PushbackReader RandomAccessFile Reader
Serial Serializable StreamCorruptedException StringReader StringWriter SyncFailedException
UncheckedIOException UnsupportedEncodingException UTFDataFormatException Writer
""".split())

ON_DEMAND = {"java.util": JAVA_UTIL, "java.io": JAVA_IO}

#: exceptions that do not have to be caught or declared
UNCHECKED = frozenset("""
ArithmeticException ArrayIndexOutOfBoundsException ArrayStoreException BufferOverflowException
BufferUnderflowException CancellationException ClassCastException CompletionException
ConcurrentModificationException DateTimeException DateTimeParseException EmptyStackException
EnumConstantNotPresentException FileSystemNotFoundException IllegalArgumentException
IllegalCallerException IllegalFormatException IllegalMonitorStateException IllegalStateException
IllegalThreadStateException IllformedLocaleException IndexOutOfBoundsException
InputMismatchException InvalidPathException LayerInstantiationException MalformedParametersException
MatchException MissingResourceException NegativeArraySizeException NoSuchElementException
NullPointerException NumberFormatException PatternSyntaxException ProviderNotFoundException
ReadOnlyBufferException RejectedExecutionException RuntimeException SecurityException
StringIndexOutOfBoundsException TypeNotPresentException UncheckedIOException
UndeclaredThrowableException UnknownFormatConversionException UnsupportedOperationException
UnsupportedTemporalTypeException WrongMethodTypeException ClosedWatchServiceException
DuplicateFormatFlagsException FormatFlagsConversionMismatchException
IllegalFormatCodePointException IllegalFormatConversionException IllegalFormatFlagsException
IllegalFormatPrecisionException IllegalFormatWidthException MissingFormatArgumentException
MissingFormatWidthException UnknownFormatFlagsException
""".split())

#: the supertypes of common JDK exceptions, so a `catch (IOException e)` is known to
#: cover a new `throws FileNotFoundException`
EXCEPTION_PARENTS = {
    "Throwable": "", "Exception": "Throwable", "Error": "Throwable",
    "RuntimeException": "Exception", "IOException": "Exception",
    "FileNotFoundException": "IOException", "EOFException": "IOException",
    "UnsupportedEncodingException": "IOException", "InterruptedIOException": "IOException",
    "ObjectStreamException": "IOException", "InvalidClassException": "ObjectStreamException",
    "InvalidObjectException": "ObjectStreamException",
    "NotSerializableException": "ObjectStreamException", "MalformedURLException": "IOException",
    "UnknownHostException": "IOException", "SocketException": "IOException",
    "SocketTimeoutException": "InterruptedIOException", "ConnectException": "SocketException",
    "NoSuchFileException": "FileSystemException", "FileSystemException": "IOException",
    "AccessDeniedException": "FileSystemException", "CharacterCodingException": "IOException",
    "ZipException": "IOException", "SSLException": "IOException",
    "ReflectiveOperationException": "Exception",
    "ClassNotFoundException": "ReflectiveOperationException",
    "NoSuchMethodException": "ReflectiveOperationException",
    "NoSuchFieldException": "ReflectiveOperationException",
    "IllegalAccessException": "ReflectiveOperationException",
    "InstantiationException": "ReflectiveOperationException",
    "InvocationTargetException": "ReflectiveOperationException",
    "InterruptedException": "Exception", "CloneNotSupportedException": "Exception",
    "TimeoutException": "Exception", "ExecutionException": "Exception",
    "URISyntaxException": "Exception", "ParseException": "Exception", "SQLException": "Exception",
    "GeneralSecurityException": "Exception", "NoSuchAlgorithmException": "GeneralSecurityException",
    "BrokenBarrierException": "Exception",
}

#: calls whose target touches files, sockets, databases or processes
IO_PREFIXES = (
    "java.io.File", "java.io.RandomAccessFile", "java.nio.file.", "java.nio.channels.",
    "java.net.", "java.sql.", "javax.sql.", "javax.net.", "java.lang.ProcessBuilder",
    "java.lang.Runtime.exec", "java.net.http.",
)
EXIT_CALLS = frozenset({"java.lang.System.exit", "java.lang.Runtime.exit",
                        "java.lang.Runtime.halt"})

#: collection methods that grow or shrink the receiver
GROWERS = frozenset({"add", "addAll", "put", "putAll", "push", "offer", "offerFirst",
                     "offerLast", "addFirst", "addLast", "putIfAbsent", "computeIfAbsent",
                     "compute", "merge", "addElement", "insertElementAt", "append"})
SHRINKERS = frozenset({"remove", "removeAll", "removeIf", "retainAll", "clear", "poll",
                       "pollFirst", "pollLast", "pop", "removeFirst", "removeLast",
                       "removeElement", "removeAllElements", "removeElementAt", "setLength",
                       "invalidate", "invalidateAll", "evict", "trimToSize"})

#: annotations that mark a method as run by a test framework
TEST_ANNOTATIONS = frozenset({"Test", "ParameterizedTest", "RepeatedTest", "TestFactory",
                              "TestTemplate", "Property", "Theory", "Example"})
TEST_LIFECYCLE = frozenset({"Before", "After", "BeforeClass", "AfterClass", "BeforeEach",
                            "AfterEach", "BeforeAll", "AfterAll", "Rule", "ClassRule"})
#: annotations that mean "a framework calls or injects this", mapped to a labelling word
FRAMEWORK_ANNOTATIONS = {
    "RequestMapping": "route", "GetMapping": "get", "PostMapping": "post",
    "PutMapping": "put", "DeleteMapping": "delete", "PatchMapping": "patch",
    "GET": "get", "POST": "post", "PUT": "put", "DELETE": "delete", "PATCH": "patch",
    "Path": "route", "MessageMapping": "handler", "ExceptionHandler": "handler",
    "EventListener": "listener", "KafkaListener": "listener", "JmsListener": "listener",
    "RabbitListener": "listener", "SqsListener": "listener", "Scheduled": "task",
    "Bean": "register", "Autowired": "register", "Inject": "register", "PostConstruct": "register",
    "PreDestroy": "register", "Provides": "register", "Subscribe": "subscribe",
    "WebServlet": "route", "WebFilter": "route",
}
FRAMEWORK_CLASS_ANNOTATIONS = frozenset({
    "Controller", "RestController", "Component", "Service", "Repository", "Configuration",
    "SpringBootApplication", "Entity", "Path", "WebServlet", "Singleton", "ApplicationScoped",
    "RequestScoped", "Module", "Mapper", "ControllerAdvice", "RestControllerAdvice",
})
#: annotations that change nothing a caller or subclass depends on
COSMETIC_ANNOTATIONS = frozenset({
    "Override", "SuppressWarnings", "SafeVarargs", "FunctionalInterface", "Generated",
    "Nullable", "NonNull", "Nonnull", "NotNull", "CheckForNull", "CheckReturnValue",
    "CanIgnoreReturnValue", "ParametersAreNonnullByDefault", "InlineMe", "Keep",
    "SuppressFBWarnings", "VisibleForTesting", "GuardedBy", "ThreadSafe", "Immutable",
    "Serial", "Documented", "API", "since", "Contract", "MustBeClosed", "ForOverride",
})


def is_unchecked_name(simple: str) -> bool | None:
    """True/False for JDK exceptions we know, None when the name says nothing."""
    if simple in UNCHECKED or simple.endswith("Error") or simple.endswith("RuntimeException"):
        return True
    if simple in EXCEPTION_PARENTS:
        chain = simple
        while chain:
            if chain == "RuntimeException":
                return True
            chain = EXCEPTION_PARENTS.get(chain, "")
        return False
    return None


def exception_chain(simple: str) -> list[str]:
    """``FileNotFoundException`` -> [itself, IOException, Exception, Throwable]."""
    out = [simple]
    cur = EXCEPTION_PARENTS.get(simple)
    while cur:
        out.append(cur)
        cur = EXCEPTION_PARENTS.get(cur)
    if len(out) == 1 and simple not in ("Throwable",):
        out += ["Exception", "Throwable"]           # unknown: assume a plain Exception
    return out
