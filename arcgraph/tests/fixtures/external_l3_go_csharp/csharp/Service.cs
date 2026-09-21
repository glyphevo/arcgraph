using System;

namespace ArcGraphDemo;

[AttributeUsage(AttributeTargets.Class)]
public sealed class ServiceAttribute : Attribute {}

public interface IRepository
{
    string Save(string value);
}

public class BaseService {}

[Service]
public class RepositoryService : BaseService, IRepository
{
    private readonly string name;

    public RepositoryService(string name)
    {
        this.name = name;
        Save(name);
    }

    public string Name => this.name;

    public string Save(string value)
    {
        return value;
    }
}

public record SaveRequest(string Value);
