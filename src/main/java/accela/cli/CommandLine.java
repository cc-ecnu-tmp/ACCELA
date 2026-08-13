package accela.cli;

import java.lang.reflect.Field;
import java.lang.reflect.ParameterizedType;
import java.util.*;

public class CommandLine {
    private final Object target;
    private String commandName = "";
    private String description = "";
    private boolean mixinStandardHelpOptions = false;
    private String version = "";

    private final List<FieldOption> options = new ArrayList<>();
    private final List<FieldParameters> parameters = new ArrayList<>();
    private final Map<String, FieldOption> optionMap = new HashMap<>();

    private static class FieldOption {
        final Field field;
        final Option annotation;

        FieldOption(Field field, Option annotation) {
            this.field = field;
            this.annotation = annotation;
        }
    }

    private static class FieldParameters {
        final Field field;
        final Parameters annotation;
        final IndexRange indexRange;

        FieldParameters(Field field, Parameters annotation) {
            this.field = field;
            this.annotation = annotation;
            this.indexRange = new IndexRange(annotation.index());
        }
    }

    private static class IndexRange {
        final int min;
        final int max;

        IndexRange(String rangeStr) {
            if (rangeStr.equals("*")) {
                min = 0;
                max = Integer.MAX_VALUE;
            } else if (rangeStr.contains("..")) {
                String[] parts = rangeStr.split("\\.\\.");
                min = Integer.parseInt(parts[0]);
                if (parts[1].equals("*")) {
                    max = Integer.MAX_VALUE;
                } else {
                    max = Integer.parseInt(parts[1]);
                }
            } else {
                min = Integer.parseInt(rangeStr);
                max = min;
            }
        }

        boolean contains(int idx) {
            return idx >= min && idx <= max;
        }
    }

    public CommandLine(Object target) {
        this.target = Objects.requireNonNull(target, "Target object cannot be null");
        inspectTarget();
    }

    private void inspectTarget() {
        Class<?> clazz = target.getClass();
        Command cmdAnnot = clazz.getAnnotation(Command.class);
        if (cmdAnnot != null) {
            this.commandName = cmdAnnot.name().isEmpty()
                    ? clazz.getSimpleName().toLowerCase(Locale.ROOT) : cmdAnnot.name();
            this.description = cmdAnnot.description();
            this.mixinStandardHelpOptions = cmdAnnot.mixinStandardHelpOptions();
            this.version = cmdAnnot.version();
        } else {
            this.commandName = clazz.getSimpleName().toLowerCase(Locale.ROOT);
        }

        // Inspect fields
        for (Field field : clazz.getDeclaredFields()) {
            if (field.isAnnotationPresent(Option.class)) {
                Option opt = field.getAnnotation(Option.class);
                FieldOption fo = new FieldOption(field, opt);
                options.add(fo);
                for (String name : opt.names()) {
                    if (optionMap.containsKey(name)) {
                        throw new IllegalStateException("Duplicate option name: " + name);
                    }
                    optionMap.put(name, fo);
                }
            } else if (field.isAnnotationPresent(Parameters.class)) {
                Parameters param = field.getAnnotation(Parameters.class);
                parameters.add(new FieldParameters(field, param));
            }
        }

        // Sort parameters by their min index to populate them in order if needed
        parameters.sort(Comparator.comparingInt(p -> p.indexRange.min));
    }

    public int execute(String[] args) {
        try {
            boolean helpRequested = false;
            boolean versionRequested = false;

            Map<Field, List<String>> parsedOptionValues = new HashMap<>();
            List<String> positionalArgs = new ArrayList<>();

            int argIndex = 0;
            while (argIndex < args.length) {
                String arg = args[argIndex];

                if (arg.startsWith("-")) {
                    // Check standard help/version first if enabled
                    if (mixinStandardHelpOptions) {
                        if (arg.equals("-h") || arg.equals("--help")) {
                            helpRequested = true;
                            argIndex++;
                            continue;
                        }
                        if (arg.equals("-V") || arg.equals("--version")) {
                            versionRequested = true;
                            argIndex++;
                            continue;
                        }
                    }

                    FieldOption fo = optionMap.get(arg);
                    if (fo == null) {
                        throw new CommandLineException("Unknown option: '" + arg + "'");
                    }

                    Field field = fo.field;
                    Class<?> type = field.getType();

                    if (type == boolean.class || type == Boolean.class) {
                        parsedOptionValues.computeIfAbsent(field, k -> new ArrayList<>()).add("true");
                    } else {
                        if (argIndex + 1 >= args.length) {
                            throw new CommandLineException("Option '" + arg + "' requires a value");
                        }
                        String val = args[++argIndex];
                        parsedOptionValues.computeIfAbsent(field, k -> new ArrayList<>()).add(val);
                    }
                } else {
                    positionalArgs.add(arg);
                }
                argIndex++;
            }

            if (helpRequested) {
                printUsage();
                return 0;
            }

            if (versionRequested) {
                printVersion();
                return 0;
            }

            // Validate required options
            for (FieldOption fo : options) {
                if (fo.annotation.required() && !parsedOptionValues.containsKey(fo.field)) {
                    StringBuilder namesSb = new StringBuilder();
                    for (String name : fo.annotation.names()) {
                        if (namesSb.length() > 0) namesSb.append(", ");
                        namesSb.append(name);
                    }
                    throw new CommandLineException("Missing required option: " + namesSb);
                }
            }

            // Populate option fields
            for (FieldOption fo : options) {
                List<String> vals = parsedOptionValues.get(fo.field);
                if (vals != null) {
                    setFieldValue(fo.field, vals);
                }
            }

            // Populate parameters
            Map<Field, List<String>> paramValues = new HashMap<>();
            for (int i = 0; i < positionalArgs.size(); i++) {
                String val = positionalArgs.get(i);
                boolean matched = false;
                for (FieldParameters fp : parameters) {
                    if (fp.indexRange.contains(i)) {
                        paramValues.computeIfAbsent(fp.field, k -> new ArrayList<>()).add(val);
                        matched = true;
                    }
                }
                if (!matched) {
                    // Positional argument did not match any parameter index
                    throw new CommandLineException("Unmatched positional argument: '" + val + "'");
                }
            }

            // Validate and populate parameter fields
            for (FieldParameters fp : parameters) {
                List<String> vals = paramValues.get(fp.field);
                if (fp.annotation.required() && (vals == null || vals.isEmpty())) {
                    throw new CommandLineException("Missing required parameter: '<" + fp.field.getName() + ">'");
                }
                if (vals != null) {
                    setFieldValue(fp.field, vals);
                }
            }

            // Run command if it implements Runnable/Callable
            if (target instanceof Runnable) {
                ((Runnable) target).run();
                return 0;
            } else if (target instanceof java.util.concurrent.Callable) {
                Object res = ((java.util.concurrent.Callable<?>) target).call();
                if (res instanceof Integer) {
                    return (Integer) res;
                }
                return 0;
            }

            return 0;

        } catch (CommandLineException e) {
            System.err.println("Error: " + e.getMessage());
            printUsage(System.err);
            return 2;
        } catch (Exception e) {
            e.printStackTrace();
            return 1;
        }
    }

    private void setFieldValue(Field field, List<String> values) throws IllegalAccessException {
        field.setAccessible(true);
        Class<?> type = field.getType();

        if (type.isArray()) {
            Class<?> componentType = type.getComponentType();
            Object array = java.lang.reflect.Array.newInstance(componentType, values.size());
            for (int i = 0; i < values.size(); i++) {
                java.lang.reflect.Array.set(array, i, convertValue(values.get(i), componentType));
            }
            field.set(target, array);
        } else if (Collection.class.isAssignableFrom(type)) {
            Class<?> itemType = String.class;
            if (field.getGenericType() instanceof ParameterizedType) {
                ParameterizedType pt = (ParameterizedType) field.getGenericType();
                if (pt.getActualTypeArguments().length > 0 && pt.getActualTypeArguments()[0] instanceof Class) {
                    itemType = (Class<?>) pt.getActualTypeArguments()[0];
                }
            }
            Collection<Object> col;
            if (type == List.class || type == Collection.class) {
                col = new ArrayList<>();
            } else if (type == Set.class) {
                col = new LinkedHashSet<>();
            } else {
                try {
                    @SuppressWarnings("unchecked")
                    Collection<Object> newInstance = (Collection<Object>) type.getDeclaredConstructor().newInstance();
                    col = newInstance;
                } catch (Exception e) {
                    col = new ArrayList<>();
                }
            }
            for (String val : values) {
                col.add(convertValue(val, itemType));
            }
            field.set(target, col);
        } else {
            if (!values.isEmpty()) {
                field.set(target, convertValue(values.get(values.size() - 1), type));
            }
        }
    }

    private Object convertValue(String val, Class<?> type) {
        if (type == String.class) {
            return val;
        } else if (type == boolean.class || type == Boolean.class) {
            return Boolean.parseBoolean(val);
        } else if (type == int.class || type == Integer.class) {
            return Integer.parseInt(val);
        } else if (type == long.class || type == Long.class) {
            return Long.parseLong(val);
        } else if (type == double.class || type == Double.class) {
            return Double.parseDouble(val);
        } else if (type == float.class || type == Float.class) {
            return Float.parseFloat(val);
        }
        return val;
    }

    public void printUsage() {
        printUsage(System.out);
    }

    public void printUsage(java.io.PrintStream out) {
        // Find maximum representation width for alignment
        int maxNameWidth = 0;
        List<HelpItem> helpOptions = new ArrayList<>();
        List<HelpItem> helpParams = new ArrayList<>();

        // Add standard help options if mixin enabled
        if (mixinStandardHelpOptions) {
            helpOptions.add(new HelpItem("-h, --help", "Show this help message and exit."));
            helpOptions.add(new HelpItem("-V, --version", "Print version information and exit."));
            maxNameWidth = Math.max(maxNameWidth, "-h, --help".length());
            maxNameWidth = Math.max(maxNameWidth, "-V, --version".length());
        }

        for (FieldOption fo : options) {
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < fo.annotation.names().length; i++) {
                if (i > 0) sb.append(", ");
                sb.append(fo.annotation.names()[i]);
            }
            if (fo.field.getType() != boolean.class && fo.field.getType() != Boolean.class) {
                sb.append("=<value>");
            }
            String optRep = sb.toString();
            maxNameWidth = Math.max(maxNameWidth, optRep.length());
            helpOptions.add(new HelpItem(optRep, fo.annotation.description()));
        }

        for (FieldParameters fp : parameters) {
            String paramRep = "<" + fp.field.getName() + ">";
            maxNameWidth = Math.max(maxNameWidth, paramRep.length());
            helpParams.add(new HelpItem(paramRep, fp.annotation.description()));
        }

        // Print header
        StringBuilder usageLine = new StringBuilder("Usage: ");
        usageLine.append(commandName);
        if (!helpOptions.isEmpty()) {
            usageLine.append(" [options]");
        }
        for (FieldParameters fp : parameters) {
            String paramRep = "<" + fp.field.getName() + ">";
            if (!fp.annotation.required()) {
                usageLine.append(" [").append(paramRep).append("]");
            } else {
                usageLine.append(" ").append(paramRep);
            }
        }
        out.println(usageLine);

        if (!description.isEmpty()) {
            out.println(description);
        }
        out.println();

        if (!helpParams.isEmpty()) {
            out.println("Positional parameters:");
            for (HelpItem item : helpParams) {
                printAligned(out, item, maxNameWidth);
            }
            out.println();
        }

        if (!helpOptions.isEmpty()) {
            out.println("Options:");
            for (HelpItem item : helpOptions) {
                printAligned(out, item, maxNameWidth);
            }
        }
    }

    private void printAligned(java.io.PrintStream out, HelpItem item, int maxNameWidth) {
        out.print("  ");
        out.print(item.name);
        int pad = maxNameWidth - item.name.length();
        for (int i = 0; i < pad + 4; i++) {
            out.print(" ");
        }
        out.println(item.description);
    }

    private void printVersion() {
        if (!version.isEmpty()) {
            System.out.println(version);
        } else {
            System.out.println("No version specified.");
        }
    }

    private static class HelpItem {
        final String name;
        final String description;

        HelpItem(String name, String description) {
            this.name = name;
            this.description = description;
        }
    }
}
